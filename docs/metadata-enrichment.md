# Metadata enrichment

TMDB enrichment is cached, rate-limited and budgeted per day. Matching and
metadata status are separate: a title can have a confirmed identity while its
poster or overview is still pending.

The optional IMDb and TVmaze hints are local inputs. The public repository does
not redistribute IMDb datasets or private matching results. A user may import
their own licensed/offline data into a private build directory.

Recommended policy:

* keep the daily budget below the account/provider limit;
* use the cache before issuing a network request;
* use one background leader and one run lock per installation;
* back off on 429/5xx responses;
* never make a search request consume a second enrichment request for the same
  title in the same round;
* expose counters and last-error class, but never expose API keys or response
  bodies.

Set `TMDB_DAILY_BUDGET` only as a safety cap. The actual configured budget is
stored in encrypted application settings and can be lower.
