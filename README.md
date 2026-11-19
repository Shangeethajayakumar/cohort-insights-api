# Cohort Insights API

A backend project that accepts text documents and creates a short summary and
keyword tags in the background. It uses simple text processing, not an AI model.
There is no frontend; you can try the API through its built-in documentation page.

## Technologies

- **Python 3.11+ and FastAPI:** handle API requests.
- **MongoDB:** stores documents, progress, and results.
- **Redis:** limits active documents per user and caches completed results.
- **Docker Compose:** runs the API, worker, and databases together.
- **Pytest:** tests the API and background processing.

## How It Works

1. A user submits a document.
2. The API saves it with the status `queued`.
3. A separate worker creates a summary, then keyword tags.
4. The user checks the document to see its progress and results.
5. Editing the content restarts processing. Results from the old content are not shown.

The summary is the first 240 characters after extra whitespace is removed.
Tags are up to five common words from that summary.

Each user can have up to three active documents, including queued ones. Users
can only access their own documents. Submitting the same content again can reuse
that user's previous completed results.

## Run with Docker

Install Docker Desktop, enable Linux containers, and use Docker Compose 2.24 or
newer. From the project folder, create your local configuration once using
PowerShell (skip this if you already have a `.env` file):

```powershell
Copy-Item .env.example .env
```

Then start the project:

```sh
docker compose up --build -d
```

This starts the API, background worker, MongoDB, and Redis.

- Open the API documentation: http://localhost:8000/docs
- Check database connectivity: http://localhost:8000/health
- View logs: `docker compose logs -f api worker`
- Stop the project: `docker compose down`

Stopping this way keeps the stored data. Put your database URLs, database name,
and API keys in `.env`, or set them as environment variables. These values are
not hardcoded in Python. Docker Compose requires `.env` for this setup.

[.env.example](.env.example) contains safe demo values. `MONGO_URL` and `REDIS_URL`
are used when running Python on your computer. `DOCKER_MONGO_URL` and
`DOCKER_REDIS_URL` use the container service names and are passed to the app as
`MONGO_URL` and `REDIS_URL` by Compose. `API_KEYS` is a JSON mapping of keys to user
IDs. Keep `.env` private: it is excluded from Git and Docker build copies. Replace
the public demo keys before using the app outside your local machine.

After changing `.env`, run `docker compose up -d api worker` to apply the changes.

## Try the API

1. Open http://localhost:8000/docs.
2. Click **Authorize**, enter `demo-alice-key`, and confirm.
3. Open `POST /documents`, click **Try it out**, and submit:

```json
{
  "user_id": "alice",
  "title": "Learning Python",
  "content": "Python pipelines generate useful insights from documents."
}
```

Copy the returned `document_id` into `GET /documents/{document_id}` and execute
the request. Check again until the status is `completed` or `failed`. Summary
and tags are returned together only after both steps finish successfully.

By default, the summary step takes 10-20 seconds and tagging takes 5-15 seconds,
plus any queue time. Each step has a simulated 10% failure chance to demonstrate
retries, with up to three attempts per step.

To change the document, use `PATCH /documents/{document_id}`:

```json
{
  "content": "Updated lesson about Python and background processing.",
  "expected_version": 1
}
```

`expected_version` is optional. When provided, it must match the current
`content_version`; otherwise, the API returns `409` to avoid overwriting a newer edit.

The second demo user is `bob`, with the key `demo-bob-key`. Bob cannot access
Alice's documents. Outside the documentation page, send the key in the header
`Authorization: Bearer demo-alice-key`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/documents` | Submit a document |
| GET | `/documents/{document_id}` | Get progress and results |
| PATCH | `/documents/{document_id}` | Update content and restart processing |
| GET | `/users/{user_id}/documents` | List a user's documents |
| GET | `/documents/by-ref/{ref}` | Find a document by its external reference |
| GET | `/health` | Check MongoDB and Redis connections |

All document endpoints require authentication. The list endpoint supports `page`,
`page_size`, and an optional `status` filter.

You can optionally include `client_doc_ref`, such as `cms:lesson-1`, when submitting
a document. It must be unique across all users. Repeating the same submission
with the same reference returns the existing document; conflicting data returns `409`.

Common errors are `401` for a missing or invalid API key, `404` for documents
you cannot access, `422` for invalid input, `429` when all three active slots are
in use, and `503` when required storage is unavailable.

## Design Decisions and Assumptions

- **Users:** I used API keys to identify users without adding a signup system.
  Alice and Bob are sample accounts; more users can be added in the configuration.
  User IDs are strings because the assignment does not require numbers. Reading
  another user's document returns `404`, just like reading a missing document.
  Looking up an external reference also requires the owner's key.
- **Background work:** a separate worker reads unfinished documents from MongoDB.
  The saved document also tells the worker what needs processing, so there is no
  separate queue message to lose. Each time a worker takes a job, it gets a unique
  code called a claim token and a time limit. After that limit, another worker can
  take over. The old token no longer allows the previous worker to save results.
- **Failures:** each stage stores its status, number of attempts, error, and result.
  If tagging fails, the worker retries tagging using the saved summary. It does
  not repeat the summary step. Editing the content with PATCH restarts both steps.
- **Three-document limit:** Redis stores one reservation token for each active job.
  Counting these tokens gives the user's active count. A small Lua script checks
  the count and adds a token together, without another request running between
  those steps. This prevents two requests from taking the last available slot.
  Reservations do not expire automatically because slow jobs must still count.
- **Reusing results:** cache keys use the user hash, content hash, and pipeline
  version, not the document ID. A hash is a fingerprint of the exact text, including
  spaces and line breaks. Cached results expire after one hour by default. If Redis
  has no cached result or cannot be read, the API checks MongoDB for a matching
  completed result. If Redis is down, work needing a new slot returns `503`;
  reading existing documents from MongoDB still works.

## Schema & Staleness Design

Each MongoDB document stores the content and both processing results together:

| Field | What it means |
| --- | --- |
| `content_version` | Starts at 1 and increases on every successful PATCH |
| `content_hash` | SHA-256 fingerprint of the current content |
| `stages.processing.result` | Summary value, plus its content version and hash |
| `stages.enriching.result` | Tags value, plus their content version and hash |
| `claim_token`, `lease_until` | The worker's permission code and its expiry time |

For example, a summary produced for version 1 must not be shown as the result of
version 2. The code checks this in three places:

1. **Update everything together.** PATCH replaces the content and hash, increases
   the version, clears both results, resets the stages, and removes the worker
   claim in one atomic MongoDB update. Atomic means readers cannot see half of
  that update. The update only goes ahead if the version and status still match
  what the API read. Otherwise, it returns `409` instead of overwriting newer work.
2. **Reject old worker writes.** Every stage write checks the document ID, content
  version, claim token, and whether the claim has expired. A worker processing version 1
   cannot save its result after PATCH has moved the document to version 2.
   Enriching also checks the summary's version and hash before using it.
3. **Show results only when both match.** The API reads the content and results
  together from one MongoDB document. It shows
   summary and tags only when the document and both stages are completed and both
   result versions AND hashes match the content. Otherwise, both outputs are `null`.

This means a response cannot contain a version-1 summary with version-2 tags or
content. If GET and PATCH happen at the same time, GET might read the whole old
document before the update, but it cannot mix old and new fields. Changing content
from A to B and back to A still gets a new version, so an old worker cannot save
its result just because the text matches again.

`results_current` is true when both results are ready and match the content.
`is_stale` is true when a stored result's version or hash does not match. It is
calculated from those values, rather than being a separate guess about freshness.
Immediately after PATCH, there are no results, so both flags are false. When a new
submission reuses cached results, both values are saved with the new document's
version and hash. PATCH always reruns processing.

## External Reference Rules

`client_doc_ref` is an optional ID supplied by a partner system. Uppercase and
lowercase letters count as different characters. I chose to make each reference
unique across all users, so one reference points to one document.

- First submission: creates a document and returns `201`.
- Same reference, same owner, same current title and content: returns the existing
  document with `200`, without starting another job.
- Same reference with different title, content, or owner: returns `409` without
  sharing document details. Use PATCH to edit a document. An old submission that
  arrives late cannot replace newer content.
- Lookup of a missing reference or another owner's reference: returns `404`.

The lookup uses the ascending index `{client_doc_ref: 1}`, with `unique: true`
and the partial filter `{client_doc_ref: {$type: "string"}}`. Documents without
a reference leave out the field and are not included in this index. MongoDB can
use the index to find a reference without checking every document. The database
also blocks duplicate references if two submissions arrive at the same time.
A conflict tells the caller that a reference is taken, but does not reveal the
document or its owner.

## At 100×

These are changes I would consider if traffic grew. They are not implemented yet.

### A User with 500K Documents

The listing indexes are `(user_id, created_at DESC, _id DESC)` and
`(user_id, status, created_at DESC, _id DESC)`. They still help MongoDB find one
user's documents in newest-first order. Having 500,000 documents does not make
the indexes stop working. But more data may need to be read from disk if it no
longer fits in memory. A very active user can also put heavy load on one server.
I would measure query time and how many index entries and documents MongoDB reads.
For lists, I would return basic details instead of the full content of every document.

### Spreading Data Across Servers

Sharding means splitting data across database servers. I would consider a shard
key made from `user_id` and a bucket calculated from the document ID. The bucket
splits a user's documents into groups that can be placed on different servers.
Using only `user_id`, even hashed, keeps that user's data together, so one large
user can overload a server. Buckets avoid that single-user limit, but listing
must combine sorted results from several groups. I would test the bucket count
and placement. MongoDB also restricts unique indexes when sharding. Keeping
references unique would need a separate reference-to-document collection split
by reference, with a way to repair failures between saving the mapping and document.

### Redis at Higher Request Rates

The Redis script still prevents requests from taking the same last slot, but it
may not be fast enough at 100 times the traffic. I would measure script time,
Redis writes to disk, and the MongoDB checks that keep active-slot records in sync.
Redis Cluster could spread different users across servers, but one user's set
still stays on one server. I would separate caching from slot tracking and add
a saved record of reservations with a repair process. A crash between reserving
a slot and saving a document can currently leave an unused slot. Automatically
expiring slots could wrongly allow extra work while earlier jobs are still running.

### Pagination

Skip/limit gets slower on later pages because MongoDB must pass over earlier
entries. I would use cursor pagination: remember the last document's
`(created_at, _id)` and ask for documents after it in newest-first order. The ID
handles documents with the same timestamp. The next request must keep the same
user and status filter. This avoids skipping more entries on every page, but
updates can still change which documents match the filter between requests.

## Improvements with More Time

- Replace public demo keys with a proper login system and secure storage for secrets.
- Add a repair tool for slots left behind after crashes. Saving to MongoDB and
  Redis happens separately, so a failed request can leave an unused slot that
  blocks new work until it is repaired.
- Run tests with real MongoDB and Redis automatically on code changes, including
  database restarts and connection failures. Test doubles cannot prove how real
  databases behave during failures or heavy traffic.
- Track waiting time, failures, retries, and differences between Redis slot counts
  and active MongoDB jobs.
- Add cursor pagination and smaller list responses, then measure performance before
  deciding how to split data across servers.
- Extend claim time limits for longer jobs and let workers finish their current
  step when shutting down. Claims currently have a fixed time limit.

## Code Structure

```text
backend/
  app.py                 Starts the FastAPI application
  worker.py              Starts background processing
  config/settings.py     Application settings
  scripts/
    common/              Authentication and logging
    core/
      handlers/          API routes
      models/            Request and response validation
      services/          Document logic and processing steps
      utils/             Shared helpers for versions and results
    db/                  MongoDB and Redis operations
tests/                   API, worker, and document-rule tests
```

To follow a request, start with
[backend/scripts/core/handlers/documents.py](backend/scripts/core/handlers/documents.py),
then read
[backend/scripts/core/services/documents.py](backend/scripts/core/services/documents.py).
The shared checks in
[backend/scripts/core/utils/versioning.py](backend/scripts/core/utils/versioning.py)
make sure results belong to the latest content.

## Run Tests

With Python 3.11 or newer installed, run these commands in Windows PowerShell:

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check backend tests
```

The default tests use simulated databases and their own test configuration, so
they do not require a local `.env` or running MongoDB and Redis. They cover
permissions, document updates, caching, active limits, and worker retries.
To test against real databases through Docker, complete the `.env` setup above
and run:

```sh
docker compose --profile test run --build --rm tests
```

## Local Development

After installing the dependencies and creating `.env` as described above, start
only the databases:

```sh
docker compose up -d mongodb redis
```

Run the API and worker in two separate PowerShell terminals, from the project folder:

```powershell
.venv\Scripts\python.exe -m uvicorn backend.app:app --reload
```

```powershell
.venv\Scripts\python.exe -m backend.worker
```