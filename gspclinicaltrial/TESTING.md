# Testing Guide

## Purpose

This file is only for credentials, access rights, and basic environment setup. The business value proposition and demo flow are documented separately in `DEMO_VALUE_PROPOSITION.md`.

## Authentication

The dashboard requires login. All users must authenticate with a Cognito account before accessing any screen.

### Cognito Pool

| Field | Value |
|---|---|
| Pool ID | `us-east-1_ZyRil52W0` |
| Region | `us-east-1` |
| App Client ID | `2ugq0tebr2iq14fv8qp18v5d1j` |
| App Client Name | `clinical-trial-dashboard` |

### Test Credentials

| Role | Email | Password | Access |
|---|---|---|---|
| **Admin** | `admin@clinicaltrial.demo` | `TrialAdmin2026!` | Full access, including Admin, Dev Ops, trial create/delete, screening rules, and demo reset |
| **Researcher** | `researcher@clinicaltrial.demo` | `Research2026!` | Screening, Analytics, Trial Builder, and Flow Editor |
| **Viewer** | `viewer@clinicaltrial.demo` | `Viewer2026!` | Screening and Analytics read access |

### Role Permissions

| Action | Admin | Researcher | Viewer |
|---|---|---|---|
| Sign in | Yes | Yes | Yes |
| View Screening | Yes | Yes | Yes |
| Run patient screening | Yes | Yes | Yes |
| View Analytics | Yes | Yes | Yes |
| View Trial Builder | Yes | Yes | No |
| Create / delete trials | Yes | Yes | No |
| View Flow Editor | Yes | Yes | No |
| View Dev Ops | Yes | No | No |
| Manage Dev Ops tests | Yes | No | No |
| View Admin | Yes | No | No |
| Manage users / groups | Yes | No | No |
| Configure screening rules | Yes | No | No |
| Reset demo data | Yes | No | No |

Admin-only nav items are hidden from non-admin users.

## Demo Reset Access

The reset action is available only to Admin users under **Admin > Demo Reset**.

Reset preserves:

1. Cognito users and roles.
2. Patient population.
3. Baseline seeded trials: `NCT00000001`, `NCT00000002`.

Reset clears:

1. Imported/custom trial configs.
2. Imported/custom trial `Questionnaire` resources in HealthLake.
3. Trial `QuestionnaireResponse` resources tied to deleted questionnaires.
4. Trial `ResearchStudy` resources tied to deleted trial IDs.
5. Screening history and analytics activity.
6. Dev Ops tests.
7. Screening rules, restored to defaults.
8. Screening and escalation queues when available.
9. Assistant chat history.

## Environment Setup

Copy `.env.example` to `.env` in `clinicaltrials/dashboard/` and fill in:

```bash
VITE_API_URL=https://<api-gateway-id>.execute-api.us-east-1.amazonaws.com/dev
VITE_API_KEY=<your-api-key>
VITE_COGNITO_CLIENT_ID=2ugq0tebr2iq14fv8qp18v5d1j
VITE_COGNITO_REGION=us-east-1
```

## Running Locally

```bash
cd clinicaltrials/dashboard
npm install
npm run dev
```

## Verification Commands

Frontend:

```bash
cd clinicaltrials/dashboard
npm run build
npm run lint
```

Backend route tests:

```bash
cd clinicaltrials
pytest tests/test_routes.py
```

Full backend test collection currently requires optional local packages such as `hypothesis` and `playwright`.

## Managing Users With AWS CLI

### Create A New User

```bash
aws cognito-idp admin-create-user \
  --user-pool-id us-east-1_ZyRil52W0 \
  --username user@example.com \
  --temporary-password TempPass2026! \
  --region us-east-1

aws cognito-idp admin-add-user-to-group \
  --user-pool-id us-east-1_ZyRil52W0 \
  --username user@example.com \
  --group-name researcher \
  --region us-east-1
```

### Reset A Password

```bash
aws cognito-idp admin-set-user-password \
  --user-pool-id us-east-1_ZyRil52W0 \
  --username user@example.com \
  --password NewPass2026! \
  --permanent \
  --region us-east-1
```

### List Users

```bash
aws cognito-idp list-users \
  --user-pool-id us-east-1_ZyRil52W0 \
  --region us-east-1
```
