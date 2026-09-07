# 115 integration and one-click transfer

115 transfer is an optional, user-configured provider integration. It is not a
proxy for anonymous or third-party accounts.

## Required credentials

* A 115 web session Cookie, acquired through the Settings QR re-authorisation
  flow or another private operator process;
* a 115 Open Platform access/refresh pair when target-directory resolution is
  performed through the Open Platform;
* an allowed target root (normally the configured OpenList 115 mount).

All three are encrypted with the Fernet master key. They are never returned by
status APIs or written to logs.

## Transfer sequence

1. The detail page sends `resource_link_id` and the displayed target path.
2. The server verifies that the link belongs to the library and is a live 115
   link.
3. The target path is resolved server-side to a current CID; a client-supplied
   PID is never trusted by itself.
4. The server performs the read-only share snapshot request using the Cookie.
5. It submits one receive request. Receive is deliberately not retried, since
   an ambiguous timeout could otherwise create a duplicate transfer.
6. The response is mapped to explicit states such as re-authentication required,
   rate limited, provider error, unknown outcome, or success.

Non-115 links use open/copy actions. They are not sent to the receive endpoint.

## Operational caveat

The web endpoints used by 115 share receive and QR re-authorisation are not a
stable public API. They may change or be restricted by the provider. Keep this
adapter replaceable and verify current terms before deploying it for a new
account.
