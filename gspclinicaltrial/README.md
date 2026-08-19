# TrialMatch360

TrialMatch360 is a clinical trial recruitment workbench that turns connected patient data into explainable patient-to-trial matching. It lets clinical teams import trials from ClinicalTrials.gov, screen patients from a linked provider/EHR population, understand eligibility decisions, and track recruitment performance across studies.

---

## Demo Highlights

- **Import trials from ClinicalTrials.gov** — Search by condition or keyword, import by NCT ID; inclusion/exclusion criteria are auto-extracted into a structured protocol ready for screening.
- **Patient screening against HealthLake** — Select a patient and trial, run the eligibility check, and review ELIGIBLE/INELIGIBLE/BORDERLINE determinations with per-criterion breakdowns.
- **Trial Builder** — Review imported trials, manage eligibility criteria, and prepare studies for screening.
- **Screening Analytics** — Aggregate eligibility rates across trials, trend data, and per-trial breakdowns.

---

## What else is in here

- **Flow Editor** — Visual node editor for customizing the screening workflow without code changes.
- **DevOps** — Test suite runner and trace viewer for validating screening pipelines and inspecting AI reasoning steps.

---

## Setup

The only required config value is your HealthLake datastore ID.

```bash
cd clinicaltrials
cp .env.example .env
# Set HEALTHLAKE_DATASTORE_ID in .env — that is the only required value
set -a; source .env; set +a
bash infra/setup.sh
```

Optional flags:

```bash
bash infra/setup.sh --with-patients    # import FHIR sample data into HealthLake
bash infra/setup.sh --with-agentcore   # deploy AgentCore MCP gateway
```

Run locally after setup:

```bash
uvicorn app:app --reload --port 8000
cd dashboard && npm install && npm run dev
```
