# Publishing SSDU

[MIT](LICENSE) covers SSDU's author-owned software, documentation and invented
test fixtures. It does not relicense provider data, metadata, paper abstracts,
models or dependencies. See the [official MIT text](https://opensource.org/license/mit).
Contributors must have permission to contribute their work under MIT; confirm
employer or institutional ownership where applicable.

The public source tree contains no captured third-party payloads. Downloads,
code-list dumps, literature corpora, source-derived fixtures, snapshots, bundles,
embeddings, results and private development records stay local. This policy also
excludes data that might permit redistribution. Users retrieve data directly
from providers and retain its source, licence and citation in their own stores.

Open access does not imply MIT licensing. For example, the
[Open Government Licence](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/)
requires attribution and excludes specified rights. Review each provider's terms,
access controls and API conditions; preserve unknown licence statements rather
than inferring permission. SSDU exports are a user action and are not permission
to redistribute the exported data.

## Prepare a source-only checkout

`publication.json` is an exact-file, SHA-256-bound review inventory. Only reviewed
source, configuration, curated documentation and independently invented fixtures
belong there. New files are excluded until explicitly reviewed. Changed approved
files fail until their digests are reviewed and updated. Notebook outputs and
attachments must be empty. The tool rejects symlinks, unsafe paths, known secret
formats, binary payloads and forbidden publication categories.

```sh
python tools/publication.py audit
python tools/publication.py export --output data/publication/review
python tools/publication.py verify data/publication/review
```

Use an absent output directory. Inspect `inventory.json`, `receipt.json` and
`tree/`, then run installation, tests and examples inside that clean tree.
The receipt confirms boundary checks, not legal ownership or universal secret
detection. Source review remains necessary, including inline test observations.
Do not run a blanket `git add .` in the working research checkout. Initialize or
update Git from the reviewed export, then inspect staged paths before publishing.
The exporter performs no Git, network, commit, repository creation or push action.

## Tests and dependencies

The inventory lists the source-only test files and excluded research tests.
Tests requiring local corpora or retained benchmark bundles are not part of the
public acceptance claim. Provider conformance and examples must use invented
responses; live provider checks retain their responses only in ignored `data/`.

Dependencies are installed separately through the lockfile, without vendoring
their source, binaries, model weights or license files into SSDU. A local review
of direct runtime package metadata found MIT, BSD-family and Apache-2.0 terms;
this is not a transitive or binary-distribution clearance. Their own licences
remain applicable. If dependencies or copied code are later bundled, preserve
the required copyright notices, licences and any applicable NOTICE files, and
review the resulting distribution separately. No vendored source directory or
copied-code attribution marker was found in the current `src/`/`tools/` review;
repository history is not available to establish authorship independently.
