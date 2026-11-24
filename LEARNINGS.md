# Compute session API

Verified against the SAS API contract and offline regression tests on 2026-11-24.

- A Compute session collection defaults to summary items. Request the full session media type in the `Accept-Item` header when a tool needs owner or state. [Get active compute sessions](https://developer.sas.com/rest-apis/compute/getSessions)
- Session deletion can return HTTP 202 while work finishes. Only HTTP 204 confirms deletion. [Delete session](https://developer.sas.com/rest-apis/compute/deleteSession)
