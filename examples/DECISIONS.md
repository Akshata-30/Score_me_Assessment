# Onboarding decision explanations

The demo submits this Finance hire twice with distinct request keys. The first submission uses v1 and the second uses v2. Replaying the first key returns the existing v1 request.

```json
{
  "employee_id": "EMP-FLAKY",
  "department": "finance",
  "employment_type": "full_time",
  "offer_accepted": true,
  "document_ref": "DOC-1042",
  "security_training_complete": false
}
```

| Scenario | What the audit records | Outcome |
|---|---|---|
| Finance hire under v1 | Employee ID and paperwork present, offer accepted, directory unavailable on attempt one and verified on attempt two. Engineering rule did not match, so the policy took the ready branch. | Ready for onboarding |
| Same hire under v2 | Same intake and directory checks. Finance rule matched. The training rule compared `security_training_complete` to `true` and failed. | HR review |
| HR decides the v2 request | The reviewer chose reject and recorded “Security training must be completed before this hire can proceed.” | Rejected |
| Missing or blank document reference | Paperwork presence check failed before the directory call. | HR review |
| Employee ID `EMP-MISMATCH` | The directory returned a mismatch on its first attempt. | HR review |
| Employee ID `EMP-OUTAGE` | After two scheduled retries, the third unavailable response exhausted the retry budget. | HR review |
| Offer declined | Offer acceptance check failed before paperwork or verification. | Rejected |

The audit trail identifies the policy version and shows each stage transition. For the v2 training decision, `from.stage` is `security_training`, `input_ref` identifies the training field, `expected` is `true`, `matched` is `false`, and `to.status` is `manual_review`. The request snapshot holds the submitted value.

Run `python3 examples/demo.py` to see both complete request histories.
