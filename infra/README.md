# infra — Phase 7

One service, one deploy target.

* `Dockerfile` — builds the FastAPI + LangGraph service.
* Deploy to Railway / Fly / Render; set every var from `.env.example` as a
  platform secret. Never bake secrets into the image.
* **Region matters.** Co-locate with Vapi and Groq — cross-region hops eat
  100–150ms of the 600–800ms budget (plan §13).
* Health check: `GET /health`.
* Turn on LangSmith tracing (`LANGCHAIN_TRACING_V2=true`) before load testing,
  so you can see which stage is spending the budget.
