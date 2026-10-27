# DirectAdmin bulk domain and subdomain NS CLI

`da_bulk_ns.py` creates DirectAdmin domains and adds delegated NS records for subdomains. It uses only the Python standard library.

It has been tested with mocked API responses and a live DirectAdmin workflow.

## Requirements

- Python 3.9 or newer
- A DirectAdmin URL using HTTPS, such as `https://server.example.com:2222`
- A DirectAdmin username and Login Key/password with permission to manage the domains

No `pip install` is required.

Never commit a real DirectAdmin password or Login Key.

## CSV format

Required columns are `domain`, `subdomain`, `ns1`, and `ns2`. Additional columns such as `ns3` and `ns4` are supported.

```csv
domain,subdomain,ns1,ns2
example.com,api,ns1.provider.net,ns2.provider.net
example.com,tracking,ns1.provider.net,ns2.provider.net
example.org,click,ns1.other.net,ns2.other.net
```

The `subdomain` value may be relative (`api`) or fully qualified (`api.example.com`). At least two distinct nameservers are required.

## Interactive usage

Run without arguments to be guided through the setup:

```bash
python3 da_bulk_ns.py
```

The program asks for:

1. CSV path
2. Number of parent domains to process concurrently
3. Whether to apply changes when `--apply` was not supplied
4. DirectAdmin URL
5. Username
6. Login Key/password

Press Enter at the apply prompt to keep dry-run mode.

## Command-line usage

Dry-run:

```bash
python3 da_bulk_ns.py \
  --csv domains.csv \
  --url https://server.example.com:2222 \
  --workers 2
```

Apply changes:

```bash
python3 da_bulk_ns.py \
  --csv domains.csv \
  --url https://server.example.com:2222 \
  --workers 2 \
  --apply
```

The username and password can also be supplied through `DA_USERNAME` and `DA_PASSWORD`. The URL can be supplied through `DA_URL`. A limited-permission Login Key is preferred.

## Run directly from GitHub

Review the repository before executing remote code. On macOS/Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/rahul4tech/directadmin_api/master/da_bulk_ns.py | python3 -
```

In Windows PowerShell:

```powershell
curl.exe -fsSL https://raw.githubusercontent.com/rahul4tech/directadmin_api/master/da_bulk_ns.py | py -3 -
```

Both commands start the interactive flow without requiring command-line arguments.

## Behavior and safety

- Dry-run is the default. Changes require `--apply` or an explicit `y`/`yes` response.
- `--workers N` keeps up to N parent-domain jobs active. As soon as one finishes, the next queued domain starts.
- Each domain’s operations remain sequential: create/check domain, read DNS, add an NS record, and verify it.
- DirectAdmin domain creation is serialized because DirectAdmin locks its domain-management area. DNS work for different domains can still overlap.
- The script checks domains through `CMD_API_SHOW_DOMAINS` for the authenticated user.
- Missing domains are created with unlimited account bandwidth/quota; SSL, CGI, and PHP options are not changed.
- Existing delegation is not overwritten. CNAME conflicts and unexpected existing NS targets are reported as failures.
- Records are reread after each NS addition for verification.
- Results are written to a timestamped `da-results-*.csv` report.
- Terminal output shows progress, per-domain start/end timestamps, duration, and total run time.
- The script does not change registrar nameservers, remove records, configure the target zone, or verify public DNS propagation.
- HTTPS certificate validation is enabled by default. Use `--insecure` only for troubleshooting.

If a domain belongs to another DirectAdmin user, use that owning account or appropriate impersonation. Admin credentials do not automatically provide global DNS-management access through these APIs.

## Files

- `da_bulk_ns.py` — CLI implementation
- `da_domains_sample.csv` — example CSV
- `README_da_bulk_ns.md` — documentation
