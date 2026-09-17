# Knowledge learning: evidence policy and cost

Learning is initiated by a skill, not a global chat-prompt mandate. Replace the
existing `epistemic-honesty` skill with
[the updated SKILL.md](skills/epistemic-honesty/SKILL.md); do not add a second
learning skill. The file in `docs/skills` is an importable template, not an
automatically installed runtime skill.

## Single-source policy

`knowledge_min_sources` now defaults to `1`. A single page qualifies only when:

- Its final HTTPS hostname matches `knowledge_single_source_hosts` (with optional
  `www.` normalization, not suffix matching or automatic subdomain trust).
- Its fetched text strongly supports the short claim: at least 0.8 lexical
  coverage, all exact numbers, and compatible negation.
- None of the fetched evidence produces a strong opposite-negation match.
- The existing private-data and high-stakes exclusions pass.

The starter host list is `home.cern`, `white-rabbit.web.cern.ch`, `sqlite.org`,
`docs.python.org`, and `developer.mozilla.org`. Review and configure that list for
your deployment. It is not a claim that all primary sources are covered or that
everything on those sites is true. Source relevance and meaning still need
evaluation by the skill. The deterministic checks are not semantic proof.

Other hosts still require at least two independently hosted, non-identical
supporting pages. Setting `knowledge_min_sources` to `2` or higher forces that
minimum even for trusted hosts. An empty trusted-host list disables the
single-source exception. Existing explicitly saved settings override defaults;
on an upgraded deployment, set `knowledge_min_sources` to `1` to opt in.

These settings live in `data/settings.json`. The model must not edit the trust
policy to make a rejected claim pass. No knowledge entries are migrated,
deleted, or retroactively reclassified by this change.

## Avoiding duplicate research

The skill supplies the actual page URLs it used:

```json
{
  "action": "learn",
  "claim": "One short, reusable fact directly supported by the cited page.",
  "query": "The narrow topic",
  "source_urls": ["https://the-actual-source.example/document"]
}
```

With `source_urls`, the knowledge tool performs no discovery search. It uses the
same public-page cache as `web_search` and `web_fetch` (currently two hours), or
fetches the specified page through the existing SSRF-guarded fetcher on a cache
miss. Only server-fetched text is evidence: neither snippets, model-supplied
quotes, nor `[CONTENT]` markers embedded in a page can establish provenance.
Redirects are attributed to the final fetched host. Old cache entries without
redirect provenance refresh once.

The tool accepts at most five supplied URLs and evaluates at most 20,000
characters per page. The skill normally sends just one URL, makes at most two
web calls (`max_pages: 1` for ordinary searches), and attempts one save per
question. Evidence stays server-side; full pages are not reinserted into the
model context by the learning tool.

Older callers without `source_urls` retain one discovery-search fallback,
fetching up to three pages by default (up to five for an explicitly higher
quorum). Validation awaits at most 20 seconds. Underlying synchronous network
work may finish after cancellation; no subsequent save occurs from that timed-out
call. A rejection does not trigger further searches when URLs were supplied.

## Verification and limitations

Only `validated: true` with a `knowledge_id` confirms a save. Zero supporting
pages still fails, regardless of the configured minimum. A compound claim with
several dates/specifications can fail even when its individual facts are true;
the skill should choose one atomic fact before calling the tool.

The updated backend must be deployed/restarted and the replacement skill must
be installed in the running Aegis instance. This does not change automatic skill
selection or prove that an uninjected skill will activate.

Security references: Python documents that [URL parsing does not validate
URLs](https://docs.python.org/3/library/urllib.parse.html#url-parsing-security);
the existing fetcher still enforces public destinations and redirect checks.
[OWASP's prompt-injection guidance](https://cheatsheetseries.owasp.org/cheatsheets/LLM_Prompt_Injection_Prevention_Cheat_Sheet.html)
supports keeping fetched page content separate from tool authority and policy.
