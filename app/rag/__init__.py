"""Per-bot knowledge base (RAG) — Phase 9 Part C.

extract.py -> chunking.py -> embeddings.py -> app/db/store.py::KnowledgeStore
is the pipeline `ingest.py` drives, backing `app/tools/knowledge_tools.py`'s
`search_knowledge` native tool. See `plans/phase9.md` Part C for the design.
"""
