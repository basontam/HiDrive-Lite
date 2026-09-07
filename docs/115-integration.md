# 115 integration and one-click transfer

115 transfer is an optional, user-configured provider integration. It is not a
proxy for anonymous or third-party accounts.

## Required credentials

* A 115 web session Cookie, acquired through the Settings QR re-authorisation
  flow or another private operator process;
* a 115 Open Platform access/refresh pair when target-directory resolution is
  performed through the Open Platform;
* an allowed target root (normally the configured OpenList 115 mount).

The Cookie and Open Platform access/refresh credentials are encrypted with the
Fernet master key. The target PID/CID are non-secret internal settings in the
SQLite `settings` table; they are omitted from status APIs and logs. None of
these values are returned to the browser.

The status endpoint only exposes whether the target PID/Open Platform pair is
configured; it intentionally does not return the numeric PID/CID or any
filesystem path. The browser sends the displayed logical path and the server
performs the path-to-CID walk immediately before the receive request.

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
