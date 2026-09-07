# Security policy

Please do not publish credentials, cookies, access codes, private links, or
production logs in a GitHub issue or pull request.

Report a suspected vulnerability privately to the repository maintainer. Include
the affected version, a short reproduction, and the smallest safe evidence
needed to investigate. Redact all tokens and personal data.

If a key or cookie may have been exposed, revoke or rotate it first, then
report the incident. The application is designed to keep the following values
outside Git: the Fernet master key, HDHive credentials and OAuth tokens, TMDB
keys, 115 web cookies, 115 Open Platform tokens, OpenList tokens, and reverse
proxy identity settings.

The 115 web endpoints used for share receive and QR re-authorisation are not a
stable public API. Treat provider failures as expected operational events and
upgrade the adapter only after reviewing the provider's current terms.
