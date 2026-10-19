# FinGuard demo files (synthetic, fictitious)

Organization `xyz`, password `FinGuard-Demo-2026!`.

## Contract (sign in as contractor@xyz.test → Submit contract)

| File | What it is |
|---|---|
| `demo-contract.pdf` | XYZ Ltd ↔ Sameer Contractor, ₹50,000/month from 1 Jul 2026 to 30 Jun 2027, paid in arrears on the 5th, **invoice required**, automatic bank transfer. Payout account: `50100000020000` / IFSC `HDFC0001234` |

After the analyst validates it and verifies the account, the approver approves it, and the admin clicks **Run scheduler cycle**, July, August and September are ready to invoice.

## Invoices (contractor → Submit invoice → pick the month → "Or upload your own invoice")

Use **one invoice per month**: only one active claim per month is allowed. Each invoice is dated just after its month, so use it for that month.

| Month | File | Amount | Scenario | Expected result |
|---|---|---|---|---|
| July | `my-invoice-july.pdf` | ₹50,000 (= contract) | **1** | Paid automatically, no approval |
| July (alt) | `alt-july-lower-45000.pdf` | ₹45,000 (below contract) | **1** | Paid automatically, ₹45,000 |
| August | `my-invoice-august-higher.pdf` | ₹55,000 (+10%) | **2** | Waits for approver@xyz.test, then paid ₹55,000 |
| August (alt) | `alt-august-8pct-54000.pdf` | ₹54,000 (+8%) | **2** | Waits for approver, then paid ₹54,000 |
| September | `my-invoice-september-20pct-higher.pdf` | ₹60,000 (+20%) | **3** | Escalated to risk@xyz.test |
| September (alt) | `alt-september-invalid-hidden-instruction.pdf` | ₹50,000, hidden "approve this" text | **3** | Invoice FLAGGED, escalated to risk officer |
| September (alt) | `alt-september-photo-50000.png` | ₹50,000, phone photo (read with OCR) | **1** | Read by OCR, paid automatically |

You can also use **Submit invoice claim** (FinGuard's own generated invoice) for any month. It's always scenario 1.

To try the **payout-account change** case: as the contractor, open the contract and use **Add payout account** with a new number. After the analyst verifies it, the next claim is escalated (scenario 3).
