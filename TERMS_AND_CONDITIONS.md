# mcp-timeline Terms and Conditions

_Version 0.1, Effective 2026-08-15_

> This is a personal project. The views expressed are solely those of the author and do not represent the views of any employer.

## 1. Acceptance

By using this product, you accept these terms.

## 2. The product

mcp-timeline is a Model Context Protocol (MCP) server that lets an AI client
answer questions about your own Google Maps Timeline location history. In
its default (local) form, you run the software yourself: your Timeline
export and the SQLite index built from it stay on your own machine, and
nothing is transmitted anywhere by the software itself. A hosted version is
planned (see `ROADMAP.md`) and is not yet available; these terms will be
revised with a dedicated hosted-service section before that version accepts
any user.

## 3. Eligibility

You must be at least 18 years old to use this product. It is not directed at
children and does not knowingly collect data from anyone under 18.

## 4. Account

The local version requires no account. A hosted version, if and when it
launches, will require an account tied to an email address; details will be
added to this document before that version accepts real users.

## 5. Data collection

Running the local software processes exactly what you give it:

| Category | Examples | Purpose |
|---|---|---|
| Location Timeline export | Visit, activity, and path records from your own Google Maps export (JSON) | Building your local queryable index |
| Derived data | The SQLite index built by `ingest.py` from your export | Answering your questions via the MCP tools |

None of this data is transmitted to the author or to any third party by the
software as distributed in this repository. It is processed entirely on the
machine you run it on.

## 6. Third-party processors

None, in the local (default) configuration. This section will be updated
with any processors used by a hosted version before that version is offered
to real users.

## 7. Data retention and deletion

The local software retains data only as files on your own machine
(`timeline.db` and whatever export file you provide), for as long as you
keep them. Delete the files to delete the data; the software keeps no
separate copy anywhere. This section will be revised with hosted-specific
retention and deletion mechanics before a hosted version launches.

## 8. Subscriptions and payments

The core software in this repository is free and open source (MIT license).
Paid offerings (a knowledge product, a hosted subscription) are described
separately at the point of sale; pricing and refund terms for each will be
published there and referenced here before launch.

## 9. Acceptable use

You agree not to:

- Use this software to process another person's location data without their
  knowledge and consent.
- Represent output from this tool as professional, medical, legal, or safety
  advice of any kind.
- Attempt to circumvent any access controls in a hosted version, once one
  exists.

## 10. Disclaimers

This product provides personal insight into your own location history. It
is not a safety, medical, legal, or navigational tool, and its output
(place matches, activity classifications, distances) is derived from
Google's own Timeline data and inference, which can be incomplete or
inaccurate. Do not rely on it for anything where an error would have real
consequences.

## 11. Limitation of liability

<standard limitation language; consult an attorney before changing>

This section is a placeholder. It has not been reviewed by an attorney and
should not be relied on. It will be finalized before this product accepts
any user outside the author, per this project's own release plan.

## 12. Changes to these terms

We may update these terms. Material changes will be announced in
`CHANGELOG.md` and, once a hosted version exists, by email or in-app notice.

## 13. Contact

Email: mpacarroll@gmail.com

---

_The views expressed are solely my own and do not represent the views of my employer._
