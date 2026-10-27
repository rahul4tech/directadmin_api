#!/usr/bin/env python3
"""Bulk DirectAdmin domain creation + subdomain NS delegation.

Python 3.9+, standard library only. Dry-run by default; --apply makes changes.
CSV headers: domain,subdomain,ns1,ns2 (additional ns3, ns4... accepted).

IMPORTANT: DirectAdmin's legacy NS-add endpoint swaps the meaning of the
name and value POST fields; this program accounts for that.
"""

import argparse
import base64
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import getpass
import json
import os
import re
import ssl
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, parse_qs, urlsplit
from urllib.request import Request, urlopen


class DAError(Exception):
    pass


def prompt(message):
    """Read interactive input from the terminal, including `curl | python3 -`."""
    for device in ("/dev/tty", "CONIN$"):
        try:
            with open(device, "r", encoding="utf-8", errors="replace") as tty:
                sys.stderr.write(message)
                sys.stderr.flush()
                return tty.readline().rstrip("\r\n")
        except OSError:
            pass
    return input(message)


LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def hostname(value, label):
    value = (value or "").strip().lower().rstrip(".")
    if len(value) > 253 or not value or any(not LABEL.fullmatch(p) for p in value.split(".")):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def fqdn(value):
    return value.rstrip(".").lower() + "."


def ns_owner(subdomain, domain):
    subdomain = hostname(subdomain, "subdomain")
    if subdomain == domain:
        raise ValueError("Subdomain must not equal the parent domain")
    if subdomain.endswith("." + domain):
        return subdomain
    return hostname(subdomain + "." + domain, "subdomain")


def normalize_owner(value, domain):
    """Canonical owner name from either relative or absolute DA record name."""
    name = str(value or "").strip().lower().rstrip(".")
    if name == "@" or name == "":
        return domain
    if name == domain or name.endswith("." + domain):
        return name
    return name + "." + domain


@dataclass(frozen=True)
class Job:
    line: int
    domain: str
    owner: str
    servers: tuple


def load_csv(path):
    jobs = []
    errors = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = {h.strip().lower(): h for h in (reader.fieldnames or []) if h}
        required = ("domain", "subdomain", "ns1", "ns2")
        if not all(h in headers for h in required):
            raise ValueError("CSV needs columns: domain,subdomain,ns1,ns2")
        server_headers = sorted((k for k in headers if re.fullmatch(r"ns[1-9][0-9]*", k)),
                                key=lambda s: int(s[2:]))
        for index, row in enumerate(reader, 2):
            if not any(str(x or "").strip() for x in row.values()):
                continue
            try:
                domain = hostname(row.get(headers["domain"]), "domain")
                if "." not in domain:
                    raise ValueError("Parent domain must have at least two labels")
                owner = ns_owner(row.get(headers["subdomain"]), domain)
                ns = tuple(dict.fromkeys(hostname(row.get(headers[h]), h)
                                             for h in server_headers
                                             if str(row.get(headers[h]) or "").strip()))
                if len(ns) < 2:
                    raise ValueError("At least two distinct nameservers are required")
                jobs.append(Job(index, domain, owner, ns))
            except ValueError as exc:
                errors.append(f"CSV line {index}: {exc}")
    if errors:
        raise ValueError("\n".join(errors))
    if not jobs:
        raise ValueError("CSV contains no nonempty rows")
    return jobs


def unpack_response(body):
    """DA legacy API returns either JSON or application/x-www-form-urlencoded."""
    value = body.strip()
    if not value:
        return {}
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    if "=" not in value:
        raise DAError("Unknown response format: " + value[:150])
    obj = parse_qs(value, keep_blank_values=True)
    return {key: values[0] if len(values) == 1 else values for key, values in obj.items()}


def check_api_success(body, description):
    obj = unpack_response(body)
    if not isinstance(obj, dict):
        raise DAError(f"{description}: unexpected API response")
    error = str(obj.get("error", "")).strip().lower()
    if error not in ("", "0", "false"):
        raise DAError(f"{description}: {obj.get('text', 'Error')}: {str(obj.get('details', ''))[:240]}")
    if "error" in obj and error in ("0", "false"):
        return str(obj.get("text", "OK"))
    if str(obj.get("success", "")).strip():
        return str(obj["success"])
    raise DAError(f"{description}: no explicit success result: {body[:200]}")


class DirectAdmin:
    def __init__(self, url, username, password, timeout, insecure):
        url = url.rstrip("/")
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc or parts.username or parts.password or parts.path:
            raise ValueError("DA URL must look like https://server.example.com:2222")
        self.url = url
        self.timeout = timeout
        self.ssl_context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        self.auth = "Basic " + base64.b64encode((username + ":" + password).encode("utf-8")).decode("ascii")

    def call(self, endpoint, method="GET", fields=None):
        fields = fields or {}
        url = self.url + endpoint
        data = None
        if method == "GET":
            url += "?" + urlencode(fields)
        else:
            data = urlencode(fields).encode("utf-8")
        req = Request(url, data=data, method=method,
                      headers={"Authorization": self.auth,
                               "Accept": "application/json",
                               "User-Agent": "DA-Bulk-NS/1.0"})
        try:
            with urlopen(req, timeout=self.timeout, context=self.ssl_context) as r:
                return r.read().decode("utf-8-sig", errors="replace")
        except HTTPError as exc:
            msg = exc.read(250).decode("utf-8", errors="replace")
            raise DAError(f"HTTP {exc.code} on {endpoint}: {msg}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise DAError(f"Connection failed on {endpoint}: {exc}") from exc

    def domains(self):
        raw = self.call("/CMD_API_SHOW_DOMAINS", fields={"json": "yes"})
        data = unpack_response(raw)
        if isinstance(data, list):
            return {hostname(str(d), "listed domain") for d in data}
        if isinstance(data, dict):
            if str(data.get("error", "0")) not in ("0", "false", "False"):
                raise DAError(f"Listing domains failed: {data}")
            listing = data.get("list[]", data.get("list", []))
            if isinstance(listing, str):
                listing = [listing] if listing else []
            if isinstance(listing, list):
                return {hostname(str(d), "listed domain") for d in listing}
        raise DAError("Could not parse CMD_API_SHOW_DOMAINS. No changes made. Response: " + raw[:250])

    def create_domain(self, domain):
        # DA docs: ubandwidth/uquota=unlimited. OFF options are left unset.
        return check_api_success(self.call("/CMD_API_DOMAIN", "POST", {
            "action": "create", "domain": domain,
            "ubandwidth": "unlimited", "uquota": "unlimited",
        }), "Create domain " + domain)

    def records(self, domain):
        raw = self.call("/CMD_API_DNS_CONTROL", fields={
            "domain": domain, "json": "yes", "full_mx_records": "yes",
        })
        data = unpack_response(raw)
        if not isinstance(data, dict):
            raise DAError(f"DNS records for {domain}: unexpected response format")
        if str(data.get("error", "0")) not in ("0", "false", "False"):
            raise DAError(f"DNS read {domain}: {data.get('text', 'Error')} {data.get('details', '')}")
        if "records" not in data or not isinstance(data["records"], list):
            raise DAError(f"DNS records for {domain} cannot be verified (no records array). "
                          "Check DNS permissions/visibility; no NS writes attempted.")
        if str(data.get("dns_ns", "2")) == "0":
            raise DAError(f"NS records are hidden for {domain} (dns_ns=0); cannot safely skip duplicates")
        return data["records"]

    def add_ns(self, domain, owner, ns):
        # Legacy DA DNS API swaps name/value when ADDING NS records.
        return check_api_success(self.call("/CMD_API_DNS_CONTROL", "POST", {
            "domain": domain, "action": "add", "type": "NS",
            "name": fqdn(ns), "value": fqdn(owner), "json": "yes",
        }), "Add NS " + owner + " -> " + ns)


def has_ns(records, domain, owner, ns):
    expected = (owner, ns)
    for r in records:
        if not isinstance(r, dict) or str(r.get("type", "")).upper() != "NS":
            continue
        name = str(r.get("name", "")).strip().lower().rstrip(".")
        target = str(r.get("value", "")).strip().lower().rstrip(".")
        if (normalize_owner(name, domain), target) == expected:
            return True
        # Some legacy DA configurations expose NS keys in swapped orientation.
        if (normalize_owner(target, domain), name) == expected:
            return True
    return False


def ns_targets(records, domain, owner):
    """Get pre-existing NS targets for this delegated owner only."""
    found = set()
    for r in records:
        if not isinstance(r, dict) or str(r.get("type", "")).upper() != "NS":
            continue
        a = str(r.get("name", "")).strip().lower().rstrip(".")
        b = str(r.get("value", "")).strip().lower().rstrip(".")
        if normalize_owner(a, domain) == owner:
            found.add(b)
        elif normalize_owner(b, domain) == owner:
            found.add(a)
    return found


def has_cname(records, domain, owner):
    return any(isinstance(r, dict) and str(r.get("type", "")).upper() == "CNAME"
               and normalize_owner(r.get("name", ""), domain) == owner for r in records)


class Report:
    COLUMNS = ("timestamp", "line", "domain", "subdomain", "action", "target", "status", "details")

    def __init__(self, path):
        self.file = open(path, "w", newline="", encoding="utf-8")
        self.csv = csv.DictWriter(self.file, fieldnames=self.COLUMNS)
        self.csv.writeheader()
        self.counts = {}
        self.lock = threading.Lock()

    def emit(self, job, action, target, status, details=""):
        with self.lock:
            self.csv.writerow({"timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               "line": job.line, "domain": job.domain, "subdomain": job.owner,
                               "action": action, "target": target, "status": status,
                               "details": str(details).replace("\n", " ")[:450]})
            self.file.flush()
            self.counts[status] = self.counts.get(status, 0) + 1
            print(f"  [{status.upper()}] {job.domain} {action} {target}" +
                  (f" — {str(details)[:130]}" if details else ""), flush=True)

    def close(self):
        self.file.close()


def _process_domain(jobs, api, known_domains, args, result, create_lock):
    """Process one parent domain sequentially; different domains may run in parallel."""
    domain = jobs[0].domain
    failures = 0
    seen = set()
    records = None
    domain_exists = domain in known_domains

    for job in jobs:
        print(f"  CSV row {job.line}: {job.domain} → {job.owner}", flush=True)
        if not domain_exists:
            if not args.apply:
                result.emit(job, "create-domain", domain, "planned")
                for ns in job.servers:
                    if (job.owner, ns) not in seen:
                        seen.add((job.owner, ns))
                        result.emit(job, "add-ns", ns, "planned", "Domain will be created first")
                continue
            try:
                # DirectAdmin locks its domain-management area globally.
                # Serialize only this operation; DNS work can remain concurrent.
                with create_lock:
                    detail = api.create_domain(domain)
                result.emit(job, "create-domain", domain, "created", detail)
                domain_exists = True
            except DAError as exc:
                result.emit(job, "create-domain", domain, "failed", exc)
                return 1
        else:
            result.emit(job, "create-domain", domain, "skipped", "Domain already owned by this account")

        if records is None:
            try:
                records = api.records(domain)
            except DAError as exc:
                result.emit(job, "read-dns", domain, "failed", exc)
                return 1

        if has_cname(records, domain, job.owner):
            result.emit(job, "add-ns", ",".join(job.servers), "failed", "Conflicting CNAME at delegated name")
            failures += 1
            continue
        existing_targets = ns_targets(records, domain, job.owner)
        unexpected_targets = existing_targets.difference(job.servers)
        if unexpected_targets:
            result.emit(job, "add-ns", ",".join(job.servers), "failed",
                        "Existing delegation uses other NS: " + ", ".join(sorted(unexpected_targets)) +
                        "; no changes made to this subdomain")
            failures += 1
            continue
        for ns in job.servers:
            key = (job.owner, ns)
            if key in seen:
                result.emit(job, "add-ns", ns, "skipped", "Repeated in this CSV")
                continue
            seen.add(key)
            if has_ns(records, domain, job.owner, ns):
                result.emit(job, "add-ns", ns, "skipped", "NS record already exists")
                continue
            if not args.apply:
                result.emit(job, "add-ns", ns, "planned")
                continue
            try:
                detail = api.add_ns(domain, job.owner, ns)
                records = api.records(domain)
                if has_ns(records, domain, job.owner, ns):
                    result.emit(job, "add-ns", ns, "added", detail)
                else:
                    result.emit(job, "add-ns", ns, "unverified", "API accepted write; record missing from readable DNS list")
                    failures += 1
            except DAError as exc:
                result.emit(job, "add-ns", ns, "failed", exc)
                failures += 1
                records = None
    return failures


def process_domain(jobs, api, known_domains, args, result, create_lock, progress, total):
    """Run one domain job and report wall-clock timing for concurrency checks."""
    domain = jobs[0].domain
    started_at = dt.datetime.now().astimezone()
    started_clock = time.monotonic()
    print(f"[START {progress}/{total} {started_at.isoformat(timespec='seconds')}] {domain}", flush=True)
    try:
        return _process_domain(jobs, api, known_domains, args, result, create_lock)
    finally:
        ended_at = dt.datetime.now().astimezone()
        elapsed = time.monotonic() - started_clock
        print(f"[END   {progress}/{total} {ended_at.isoformat(timespec='seconds')}] {domain} — {elapsed:.2f}s", flush=True)


def main():
    p = argparse.ArgumentParser(description="Batch-create DirectAdmin domains and delegate subdomain NS records")
    p.add_argument("--csv", help="Input CSV (domain,subdomain,ns1,ns2)")
    p.add_argument("--url", default=os.environ.get("DA_URL"), help="https://host:2222 or DA_URL")
    p.add_argument("--username", default=os.environ.get("DA_USERNAME"), help="DirectAdmin user or DA_USERNAME")
    p.add_argument("--apply", action="store_true", help="Actually create domains and NS records; default is dry-run")
    p.add_argument("--workers", type=int,
                   help="Parent domains to process concurrently (default: 1)")
    p.add_argument("--report", help="Results CSV path")
    p.add_argument("--timeout", type=int, default=25, help="Network timeout per request in seconds")
    p.add_argument("--insecure", action="store_true", help="Disable HTTPS certificate validation (unsafe)")
    args = p.parse_args()

    if args.timeout < 1 or args.timeout > 300:
        p.error("--timeout must be in the range 1..300")
    if not args.csv:
        args.csv = prompt("CSV file path: ").strip()
        if not args.csv:
            p.error("CSV file path is required")
    jobs = load_csv(args.csv)
    print(f"Parsed {len(jobs)} CSV rows, {len(set(x.domain for x in jobs))} parent domains.")
    if args.workers is None:
        worker_text = prompt("How many parent domains to process at once [1]: ").strip()
        try:
            args.workers = int(worker_text or "1")
        except ValueError:
            p.error("Worker count must be an integer")
    if args.workers < 1 or args.workers > 32:
        p.error("--workers must be in the range 1..32")
    if not args.apply:
        try:
            answer = prompt("No --apply supplied. Apply changes instead of dry-run? [y/N]: ").strip().lower()
        except EOFError:
            answer = ""
        args.apply = answer in ("y", "yes")
    print("MODE:", "APPLY (DNS changes permitted)" if args.apply else "DRY RUN (read only)")
    url = args.url or prompt("DirectAdmin URL (https://server:2222): ").strip()
    username = args.username or prompt("DirectAdmin username: ").strip()
    password = os.environ.get("DA_PASSWORD") or getpass.getpass("DirectAdmin Login Key / password: ")
    if not username or not password:
        raise ValueError("DirectAdmin credentials are required")
    if args.insecure:
        print("WARNING: HTTPS certificate validation is DISABLED. Use only when essential.", file=sys.stderr)
    api = DirectAdmin(url, username, password, args.timeout, args.insecure)
    report_path = args.report or "da-results-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S") + ".csv"
    result = Report(report_path)
    failures = 0
    run_started_at = dt.datetime.now().astimezone()
    run_started_clock = time.monotonic()
    print(f"Run started: {run_started_at.isoformat(timespec='seconds')}")
    try:
        print("Checking DirectAdmin domain list...")
        known_domains = api.domains()  # Fail closed if not authorized/parseable.
        grouped = {}
        for job in jobs:
            grouped.setdefault(job.domain, []).append(job)
        create_lock = threading.Lock()
        pool = ThreadPoolExecutor(max_workers=min(args.workers, len(grouped)))
        futures = []
        try:
            total_domains = len(grouped)
            futures = [pool.submit(process_domain, domain_jobs, api, known_domains, args, result,
                                   create_lock, progress, total_domains)
                       for progress, domain_jobs in enumerate(grouped.values(), 1)]
            for future in as_completed(futures):
                failures += future.result()
        except KeyboardInterrupt:
            for future in futures:
                future.cancel()
            # Running HTTP requests cannot be force-killed safely. Let them
            # finish before closing the report file, while queued work is
            # cancelled.
            pool.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
    finally:
        result.close()
    run_ended_at = dt.datetime.now().astimezone()
    run_elapsed = time.monotonic() - run_started_clock
    print(f"Run ended:   {run_ended_at.isoformat(timespec='seconds')}")
    print(f"Total time:  {run_elapsed:.2f}s")
    print(f"\nReport: {Path(report_path).resolve()}")
    print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(result.counts.items())))
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (DAError, ValueError, FileNotFoundError, PermissionError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
