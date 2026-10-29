# Working on PD01 Printer for macOS

- If the user asks you to install and start this repository, read `INSTALL.md` and carry out the local commands yourself when authorized tools are available. Report software readiness separately from printer readiness.
- This project targets macOS and Python 3.12. Keep dependencies in the project `.venv`; use the hashed `requirements.lock` for a reproducible installation. Do not replace the user's system Python or copy credentials into files.
- Device selection, calibration, jobs, previews, and logs are local to each user. Never commit those files, actual device IDs, print content, or credentials.
- Physical printing must go through the existing local AppKit confirmation window. Never click its approval button for the user, add an approval bypass, replay an attempted job, or automatically retry an uncertain send.
- Prefer preparation, read-only status, and mocked transports for verification. A successful BLE send, status reply, or HTTP health response is not proof of physical printing.
- Preserve upstream MIT attribution. Keep changes focused; run `python -m unittest discover -s tests -v` and validate changed CLI/MCP paths. Tests must not connect to real Bluetooth devices or approve real print jobs.
- When dependency inputs change, regenerate the hash lock, update its source digest, and validate a fresh installation before publishing.
