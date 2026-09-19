# Clarifying Questions

These are the accounting and workflow questions I would send before starting, to avoid guessing where the mock data is intentionally ambiguous.

1. **Intercompany settlement handling**  
   If a manual adjustment debits and credits the same intercompany account, should that always be escalated, or are there approved counterparty conventions that are simply omitted from the mock data?

2. **Approval policy for unmapped accounts**  
   If a journal line references an account that is not in the current COA but matches a renamed or deprecated prior-period account, should the prototype reject it outright or route it to a human mapping review queue?

3. **Mapping governance and separation of duties**  
   Who is allowed to approve a new account mapping or alias, and must that approver be different from the person who prepared the close or submitted the adjustment?