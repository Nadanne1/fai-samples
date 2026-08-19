# TrialMatch360 Demo Value Proposition

## Executive Summary

TrialMatch360 demonstrates a modern recruitment workflow where patient-to-trial matching moves from manual outreach to data-driven screening. In the old model, recruitment depended on teams visiting hospitals and clinics, explaining study conditions, waiting for referrals, and manually checking patient charts. In this model, the connected patient record already exists in HealthLake through provider and EHR linkages, so study teams can look up a trial, identify likely candidates, explain eligibility decisions, and monitor recruitment performance from one workflow.

The core value proposition is simple:

**Turn connected clinical data into explainable trial recruitment decisions.**

## Business Problem

Clinical trial recruitment is often slow, expensive, and operationally fragmented. Sponsors and study teams typically rely on site relationships, manual chart review, physician referrals, and broad outreach to find candidates. This creates delays, missed eligible patients, inconsistent screening quality, and limited visibility into why candidates do or do not qualify.

When patient data is already linked through providers and EHR systems, the recruitment workflow can change. Instead of asking every hospital or clinic to manually search for candidates, the study team can screen the available patient population against active trial criteria and focus human effort on the best-fit patients.

## Four Core Capabilities

### 1. Import Trial

The demo starts by importing a study from ClinicalTrials.gov. The business purpose is to quickly turn an external study record into a usable internal screening object.

What this shows:

1. Search for a trial by NCT ID, keyword, condition, status, or phase.
2. Review the trial summary and eligibility criteria.
3. Import the trial into the study library.
4. Make the trial available for candidate screening.

Business value:

1. Reduces study setup time.
2. Standardizes trial intake.
3. Gives teams a reusable study library.
4. Creates a repeatable starting point for screening candidates.

### 2. Screen Patient

After a trial is imported, the user selects a patient and screens that patient against the trial. The business purpose is to replace manual chart review with a fast, consistent first-pass eligibility assessment.

What this shows:

1. Select a patient from the connected population.
2. Select the trial to screen against.
3. Run eligibility screening.
4. Generate a candidate-level determination.

Business value:

1. Accelerates patient identification.
2. Reduces manual screening burden.
3. Helps coordinators prioritize likely candidates.
4. Supports recruitment at population scale instead of one site at a time.

### 3. Explainability

The screening result is not just a yes/no answer. The demo shows why the patient did or did not match the trial criteria.

What this shows:

1. Final determination: eligible, ineligible, or borderline.
2. Criteria passed.
3. Criteria failed.
4. Total criteria evaluated.
5. Patient context and screening trace.

Business value:

1. Builds reviewer trust in the recommendation.
2. Helps coordinators understand next steps.
3. Makes borderline cases easier to triage.
4. Supports auditability and clinical review.

### 4. Analytics

The demo ends by showing recruitment performance across the screening workflow. The business purpose is to give teams visibility into funnel health and study-level recruitment outcomes.

What this shows:

1. Total screenings.
2. Unique patients screened.
3. Eligible, ineligible, and borderline counts.
4. By-trial performance.
5. Recent screening activity.

Business value:

1. Tracks recruitment funnel progress.
2. Identifies which trials have candidate supply.
3. Shows where criteria may be too restrictive.
4. Helps leadership prioritize outreach and site engagement.

## Demo Flow

### Step 1: Start With The Old Way

Open with the current business reality:

> Traditionally, clinical trial recruitment depends on outreach to hospitals and clinics, referral networks, and manual chart review. Teams share study criteria and wait for sites to identify possible candidates. That process is slow, inconsistent, and hard to scale.

Then introduce the new assumption:

> In this demo, HealthLake already contains linked patient records from providers and EHRs. That means the question changes from “which clinic might know a patient?” to “which patients in the connected population match this trial?”

### Step 2: Import A Trial

Show ClinicalTrials.gov search and import.

Narration:

> First, we bring a trial into the recruitment workflow. The study team can search for a trial, review the study details, and import the eligibility criteria so the trial becomes screenable.

### Step 3: Screen A Patient

Go to Screening, pick a patient, select the imported trial, and run screening.

Narration:

> Now we select a patient from the connected population and screen them against the imported trial. Instead of manually reviewing the chart, the system evaluates the patient record against the trial criteria and returns a first-pass recruitment decision.

### Step 4: Explain The Decision

Show the result card, criteria breakdown, clinical context, and trace.

Narration:

> The important part is not only the determination. The team can see which criteria passed, which failed, and what patient context supported the result. That makes the recommendation usable by coordinators, investigators, and clinical reviewers.

### Step 5: Review Analytics

Go to Analytics and show the recruitment funnel.

Narration:

> At the portfolio level, teams can monitor recruitment performance: how many patients were screened, how many were eligible, which trials are producing candidates, and where follow-up is needed.

## Recommended Demo Message

Use this as the simple verbal summary:

> TrialMatch360 turns connected patient data into an explainable recruitment workflow. Instead of relying only on site-by-site outreach and manual chart review, teams can import a trial, screen patients from the linked population, understand eligibility decisions, and track recruitment performance across studies.

## What To Keep In The Foreground

Lead with these four capabilities:

1. Import trials.
2. Screen patients.
3. Explain eligibility.
4. Track analytics.

Keep these as supporting capabilities:

1. Trial Builder: useful for reviewing and managing the study library.
2. Flow Editor: useful for workflow governance and process design.
3. Dev Ops: useful for demo operations and validation.
4. Admin: useful for users, roles, and demo reset.

## What Not To Overemphasize

Do not lead the business story with infrastructure, model names, or implementation details. The strongest narrative is not the technology stack; it is the change in the recruitment operating model:

**from manual site-driven candidate discovery to connected-data-driven patient-to-trial matching.**
