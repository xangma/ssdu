# Discovery research

SSDU's discovery benchmarks explore how retained metadata can help readers find
datasets and assemble evidence. They compare lexical, semantic and hybrid ranking
on frozen corpora. This experimental workflow is separate from the production
registry and REST/MCP search.

## Evidence and label status

The experiments preserve metadata, questions, evidence references, ranking
settings, splits, vectors and input hashes. Original benchmark labels, an
independent agent review and a later agent-adjudicated overlay remain separate
versions. **Human validation remains pending.** These are not human-collected
queries or human-validated ground truth.

The latest overlay concerns a selected pool of 30 questions and 607 candidate
pairs. It retains source-backed decisions and unresolved suitability. Scores
describe that pool and label version; they do not establish global catalogue
coverage, representative user performance or a production engine improvement.

Relevance, documented study use and analytical suitability are assessed
separately. Suitability is eligible, ineligible or unknown. Missing evidence
remains unknown, while a known release, geography, aggregation or measure
mismatch has an explicit reason. Unknown suitability is not a negative label.

## Method

Discovery should preserve exact identifiers and searchable metadata fields,
then check whether candidate coverage meets the question's requirements. A
collection question may need several complementary datasets; one relevant hit
does not establish completeness. Generated descriptions must remain distinct
from publisher definitions.

Frozen replay consumes retained vectors and evidence without provider requests
or model execution. Rebuilding corpora, enriching metadata or regenerating
vectors is a separate acquisition/computation task. Preserve the exact corpus,
ranking configuration and label version when comparing results. Research corpora,
bundles and provenance records stay in private local working files, excluded from
source publication. This manual contains a curated explanation; the private
experiment inputs are not supplied by the source-only checkout.

## Research grounding

- [ACORDAR 2.0](https://doi.org/10.1145/3626772.3657866) studies dataset discovery
  and supports testing a strong fielded lexical baseline.
- [THYME](https://aclanthology.org/2025.emnlp-main.1409/) studies field-aware hybrid
  table retrieval; its setting differs from UK catalogue discovery.
- [DPDisc](https://arxiv.org/html/2510.21737v4) evaluates retrieval of collections,
  supporting explicit measurement of complementary asset coverage.
- [DCAT 3](https://www.w3.org/TR/vocab-dcat-3/) informs catalogue, distribution,
  version, coverage and access descriptions.
- [DDI](https://ddialliance.org/ddi-lifecycle) provides study, variable, question
  and population context; [SDMX](https://sdmx.org/standards-2/) provides statistical
  concepts, dimensions and code lists.

These sources motivate evaluation choices; their published results are not SSDU
results. Shared terms, textual similarity and citation evidence do not establish
equivalent measures or permission to acquire data.

## Interpret coverage carefully

Keep native dataset identities, release variants and geography vintages distinct.
Provider-wide metadata does not authenticate every requested extract or a
historical publication vintage. A no-answer case means no suitable record in the
frozen corpus, rather than proof that no data exist elsewhere. See
[provenance and interpretation](../concepts/provenance.md) for the checks required
before using a discovery result in analysis.
