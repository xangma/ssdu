# Research on finding datasets

Finding a dataset with a promising title is only the first step. Does it cover
the right people, places and dates? Does it measure what your question asks?
Would several datasets be needed together?

SSDU's discovery research explores ways to help with these questions. It compares
search based on words (**lexical search**), search based on learned text
representations (**semantic search**) and combinations of the two (**hybrid
search**).

These experiments are separate from the current application. SSDU's registry
search uses catalogue descriptions saved locally; it does not yet offer the
experimental semantic or hybrid search as a production feature.

## How results are assessed

The experiments use saved metadata, research-inspired questions and references
for the expected dataset links. They keep the input versions and search settings
so that comparisons can be repeated without downloading new evidence.

The questions and relevance decisions include generated and agent-reviewed
material. **Human validation is still pending.** Results should be read as
experimental findings on selected inputs, rather than a measure of performance
for all UK datasets or all researchers.

Three different judgments matter:

- **Relevance:** does the description relate to the question?
- **Documented use:** is there evidence that a study used this dataset?
- **Suitability:** do its measures and coverage meet this question's needs?

Missing evidence means suitability is unknown. A relevant title, a paper citation
or a high search score does not establish that the data are suitable, available
to download or permitted for your intended use. An unsuccessful search in a
saved catalogue does not prove that no suitable dataset exists elsewhere.

## Research informing the work

- [ACORDAR 2.0](https://doi.org/10.1145/3626772.3657866) studies dataset discovery
  and supports testing a strong baseline using words in specific metadata fields.
- [THYME](https://aclanthology.org/2025.emnlp-main.1409/) studies hybrid table
  retrieval; its setting differs from UK catalogue discovery.
- [DPDisc](https://arxiv.org/html/2510.21737v4) evaluates retrieval of collections,
  including whether complementary datasets are found together.
- [DCAT 3](https://www.w3.org/TR/vocab-dcat-3/) informs descriptions of datasets,
  versions, coverage and access.
- [DDI](https://ddialliance.org/ddi-lifecycle) provides study, variable, question
  and population context; [SDMX](https://sdmx.org/standards-2/) provides statistical
  concepts, dimensions and code lists.

These publications inform the methods; their results are not SSDU results. The
research inputs are separate from the published code and teaching examples.
Before using any discovered dataset, review its
[source information and interpretation limits](../concepts/provenance.md).
