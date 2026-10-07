# Contributing

Report general bugs and improvements in https://github.com/NatChung/orchd/issues. Include your macOS, Python and agent CLI versions, the orchd commit, reproduction steps, expected behavior, and a minimal synthetic example.

Public issues, pull requests and logs must exclude credentials, customer names or identifiers, personal information, internal URLs, connector settings, and machine-specific private paths. Keep sensitive operational details in an access-controlled private tracker; existing private users can use orchd-archive. Link a sanitized public issue only when the general problem can be described safely. Never include secrets in either tracker; report a possible exposure privately to a maintainer and rotate affected credentials.

Use an isolated branch and submit a pull request for a different reviewer. Run `python3 -m unittest discover -s tests` before submitting code changes. Check every changed file and commit message for private data. Use fictional fixtures and link to third-party material unless redistribution rights are established.
