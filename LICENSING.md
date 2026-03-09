# Licensing

This repository contains a mix of upstream-derived code and original additions.

It is not accurate to relicense the entire repository under a new "personal research only" license, because substantial parts of the codebase are derived from the upstream TradingAgents project and remain subject to Apache License 2.0.

## License Map

Apache License 2.0 applies to upstream-derived and mixed derivative components, including:

- `tradingagents/`
- `cli/`
- `api/`
- `main.py`
- `test.py`
- `assets/` unless otherwise noted
- other files derived from the original TradingAgents framework

PolyForm Noncommercial 1.0.0 applies to original additions created for this fork, including:

- `frontend/`
- `deploy/`
- `README.md`
- `README.zh-CN.md`
- `README.en.md`
- `CHANGELOG.md`

If a file contains its own license header or a neighboring `LICENSE` file, that more specific notice controls for that file or directory.

## Practical Meaning

- You may continue to use, modify, and redistribute Apache-2.0 parts under Apache-2.0.
- You may use the original additions listed above only for noncommercial purposes under PolyForm Noncommercial 1.0.0.
- Commercial operation, paid hosting, paid API access, SaaS packaging, or resale of the PolyForm-licensed additions is not permitted without separate permission from the maintainers.

## Intent

This fork is intended primarily for:

- local personal research
- private experimentation
- noncommercial academic or hobby use

This repository is not offered as investment advice, trading advice, or a managed commercial service.

## Upstream Attribution

This project is based on the original TradingAgents framework by Tauric Research:

- Upstream repository: <https://github.com/TauricResearch/TradingAgents>
- Upstream paper: <https://arxiv.org/abs/2412.20138>

See `NOTICE` for attribution details.
