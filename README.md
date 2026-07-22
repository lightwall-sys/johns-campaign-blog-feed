# John's Campaign blog feed

A scheduled JSON mirror of recent posts from the official [John's Campaign blog](https://johnscampaign.org.uk/blog/).

The workflow checks the source every six hours and publishes:

- `feed.json` — recent post metadata
- `status.json` — update status and timestamps

A manual update can be started from **Actions → Update feed → Run workflow**.

If an update cannot be validated, the last valid feed is retained.

The MIT licence covers the repository code. Text and images referenced by the generated feed remain the property of their respective owners.
