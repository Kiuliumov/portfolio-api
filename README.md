# AWS Contact Message API

Python 3.12 + AWS SAM + API Gateway HTTP API + Lambda + DynamoDB + SES notifications.

## Routes

| Method | Route | Access | Description |
|---|---|---|---|
| POST | `/messages` | Public | Validate and store a contact form message |
| GET | `/messages?limit=25&cursor=...` | Admin Token | List newest messages, paginated |
| GET | `/messages/{id}` | Admin Token | Retrieve a message |
| PATCH | `/messages/{id}` | Admin Token | Set status to `new`, `read`, `replied`, or `archived` |


## Requirements

- AWS account and credentials configured for deployment
- AWS SAM CLI
- AWS CLI v2
- Docker (for `sam build --use-container`, optional but recommended)
- Python 3.12 for local testing
- Optional SES sender/recipient addresses verified in SES

<img width="1624" height="494" alt="image" src="https://github.com/user-attachments/assets/5b79538e-3eac-4d90-81df-84085664a187" />
