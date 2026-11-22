# Demo walkthrough (about 8 minutes)

Start with `start.bat` or `python run.py --reset --demo`. Two applications are already loaded.

1. **Overview.** Show the pipeline counts, "Needs your attention", recent activity and the KPI cards.
2. **Happy path.** Open the live application.
   - **Checks** tab: the four providers answered by callback.
   - **History** tab: every step, with identifiers masked.
   - The header shows the merchant ID.
3. **PAN passes, GST fails.** Open the GST-cancelled case.
   - **Documents** tab: the GSTIN on the certificate matches the form, but the GST portal says the registration is cancelled.
   - Click **Get a suggestion**. The copilot advises caution and cites evidence.
   - Add a note and click **Approve**. The approval waits for a second reviewer.
   - Switch to Vikram (bottom left) and confirm. The merchant goes live.
4. **Merchant portal with AI.** Go to `/apply`.
   - Click **New application**, then **Fill with test details**.
   - On each document, click **Make a test document**, then submit.
   - In the console, the **Documents** tab shows each field read by the AI, with its confidence.
5. **Blurred or conflicting document.**
   - In the portal, make the cheque with blur set to 4–5 and submit. The merchant is asked for a clearer copy.
   - Send a sharp copy from the status page.
   - Optional: tick **Different PAN than the form** or **Hide an instruction to the AI**. The case goes to review.
6. **Failures** (System page).
   - PAN provider → **Sends every answer 3 times**: the duplicates are ignored.
   - Bank provider → **Slow**: the check times out, retries, and the late answer is accepted.
   - Before approving a case, open **More tools** → **Change bank**: the bank check re-runs.
   - Merchant ID service → **Always fails**: the case stops at *Account setup failed*. Set it back to normal and click **Retry merchant ID**.
   - GST provider → **Down**: the check is retried, then lands in the dead-letter list. Set it back to normal and click **Replay**.
7. **Metrics.** Show time in each stage, the exception rate, how documents were read and provider latency.
8. **Close.** Explain what is simulated, what is real, and the limitations listed in the README.
