# VeriPay --- Complete Project Specification for Coding Agent

**Project:** VeriPay\
**Category:** Wallet-based escrow, logistics verification, and
settlement for physical commerce\
**Primary network:** NEAR Protocol\
**Backend:** Python / Django\
**Frontend:** Django Templates + HTMX + Alpine.js + Tailwind CSS\
**Smart contract:** Rust on NEAR\
**External verification:** OutLayer TEE\
**MVP approach:** Testnet-first, no AI, no NEAR Intents, no fiat payouts

------------------------------------------------------------------------

## 1. Product Overview

VeriPay is a wallet-based, logistics-integrated escrow and settlement
platform for physical commerce.

It allows a buyer and seller to create a programmable transaction
agreement, lock the buyer's funds in a NEAR smart contract, ship the
goods through a logistics provider approved by VeriPay, verify
carrier-reported delivery information through OutLayer, and settle the
agreed allocations.

The platform coordinates three economic participants:

1.  **Buyer** --- accepts the agreement and funds the escrow.
2.  **Seller** --- creates the agreement and ships the goods.
3.  **Logistics provider** --- handles delivery and receives its agreed
    fee at the specified milestone.

VeriPay also receives a clearly disclosed platform fee.

### Core value proposition

Buyers do not have to send money directly to an unknown seller before
goods are shipped. Sellers receive assurance that the buyer has funded
the transaction. Logistics costs and platform fees are agreed in
advance, and settlement follows explicit, auditable rules.

### Central workflow

**CONNECT WALLET → CREATE AGREEMENT → ACCEPT → FUND ESCROW → SHIP GOODS
→ VERIFY DELIVERY → CONFIRM RECEIPT → SETTLE PAYMENTS**

### Important product boundaries

-   VeriPay is not a carrier and does not physically inspect parcels.
-   OutLayer attests to the execution of verification code and its
    inputs and outputs. It does not independently observe the parcel or
    prove its contents or condition.
-   The carrier's tracking API remains the source of shipment status
    information.
-   A carrier delivery scan does not prove that the buyer received the
    correct or undamaged item.
-   Buyer confirmation is a separate settlement condition.
-   Do not automatically release the seller's funds merely because the
    buyer is silent.
-   The MVP pays NEAR-compatible wallets using a supported testnet
    fungible token. It does not pay Nigerian bank accounts.
-   Do not claim real carrier partnerships or production integrations
    unless access has actually been obtained.
-   Any simulated carrier data must be clearly labelled as demo data.
-   There is no AI feature in the MVP.

------------------------------------------------------------------------

## 2. MVP Scope

### Included

-   NEAR wallet connection and wallet ownership verification.
-   Wallet-based buyer and seller identities.
-   Escrow agreement creation and acceptance.
-   Optional buyer-wallet restriction at creation.
-   Shareable invitation links.
-   Wallet-based escrow discovery and dashboards.
-   NEAR smart contract escrow and token funding.
-   Explicit seller, logistics, and platform allocations.
-   Logistics partner registration and admin approval.
-   Approved logistics provider directory.
-   Demo Carrier integration with clearly labelled simulated tracking.
-   Shipment lifecycle and tracking timeline.
-   OutLayer integration for TEE-based verification, subject to the
    actual supported SDK and submission mechanism.
-   Buyer receipt confirmation or problem reporting.
-   Smart-contract settlement and refund paths.
-   Audit trail and transaction history.
-   Django admin tools.

### Explicitly out of scope

-   AI or agentic features.
-   NEAR Intents.
-   Cross-chain settlement.
-   Fiat bank payouts.
-   Production integrations with multiple real logistics companies.
-   Full dispute arbitration or mediation.
-   Insurance claims.
-   Mobile applications.
-   Merchant subscriptions.
-   Advanced reputation systems.
-   Production KYC workflows.

### Future roadmap

The architecture should leave room for real carrier integrations, fiat
payout providers, NEAR Intents, additional supported tokens, marketplace
integrations, and optional AI-assisted agreement creation. These are not
MVP dependencies.

------------------------------------------------------------------------

## 3. Users, Identity, and Wallet Access

### Wallet-based identity

A connected NEAR wallet is the user's transaction identity. Users should
be able to see all escrows associated with their wallet.

The dashboard should separate:

-   **Buying:** escrows where the connected wallet is the buyer.
-   **Selling:** escrows where the connected wallet is the seller.
-   **Invitations:** agreements awaiting the user's acceptance.
-   **Active shipments:** funded transactions in the delivery process.
-   **Completed:** settled transactions.
-   **Payment history:** relevant funding and payout records.

No mandatory email/password account is required for the core transaction
workflow.

### Wallet ownership verification

Never trust a wallet address supplied by the browser as proof of
identity.

Implement a signed challenge flow:

1.  Backend creates a cryptographically random, single-use nonce.
2.  Challenge includes the expected domain/origin, wallet address, issue
    time, and expiry.
3.  User signs the challenge with their wallet.
4.  Backend verifies the signature using the appropriate NEAR
    wallet/account verification method.
5.  Backend marks the nonce as consumed.
6.  Backend creates or retrieves the wallet identity and issues an
    authenticated session.

Protect against replay, expired challenges, wrong origins, and reused
nonces. Never store wallet private keys or seed phrases.

### Buyer invitation and authorization

The seller creates an escrow and receives a unique shareable invitation
link.

There are two supported cases:

**Case A --- Buyer wallet known at creation** - Seller enters the
buyer's wallet address. - The contract restricts acceptance and funding
to that wallet. - Other wallets must not be able to accept the
agreement.

**Case B --- Buyer wallet not known** - Seller creates an invitation
without preassigning a buyer. - Seller shares the invitation link. - The
recipient opens the link and connects a NEAR wallet. - The recipient
reviews the complete agreement and accepts it. - The contract records
that wallet as the authorized buyer.

The invitation link is only a locator for a pending agreement. It is not
authorization by itself. Acceptance must be wallet-authenticated and
enforced by the contract.

After acceptance, the buyer can discover the escrow from their wallet
dashboard without needing the invitation link again.

------------------------------------------------------------------------

## 4. Escrow Agreement

An agreement should contain, at minimum:

-   Unique escrow ID.
-   Seller wallet.
-   Buyer wallet, if known at creation.
-   Product name.
-   Product description.
-   Product price.
-   Logistics provider or provider-selection rule.
-   Logistics fee.
-   VeriPay platform fee.
-   Total funding amount.
-   Token contract and token denomination.
-   Shipment and delivery terms.
-   Dispatch deadline.
-   Expected delivery window, where applicable.
-   Buyer inspection/confirmation period.
-   Settlement conditions.
-   Refund and failure conditions.
-   Creation timestamp.
-   Agreement version and terms hash.

The buyer must review and accept the exact terms before funding. Once
funded, financial terms and payout beneficiaries must not be silently
changed.

Large descriptions, photos, and supporting documents should remain
off-chain. Store a content hash or stable reference on-chain when
useful.

------------------------------------------------------------------------

## 5. Transaction Lifecycle

### Main lifecycle

1.  Seller connects wallet.
2.  Seller creates an agreement.
3.  VeriPay creates a pending invitation.
4.  Buyer opens the invitation and connects wallet.
5.  Buyer reviews and accepts the agreement.
6.  Buyer funds the escrow contract.
7.  Seller selects an approved logistics provider.
8.  Provider accepts the shipment request.
9.  Shipment and tracking reference are registered.
10. Carrier tracking events are received or polled.
11. OutLayer verification code checks the carrier-reported delivery
    information.
12. An authenticated verification result is recorded through the
    authorized NEAR mechanism.
13. Buyer inspects the goods.
14. Buyer confirms receipt or reports a problem.
15. Contract settles eligible allocations according to the agreement.

### Escrow states

Use a controlled state machine. Suggested states:

-   `DRAFT`
-   `PENDING_ACCEPTANCE`
-   `APPROVED`
-   `FUNDED`
-   `SHIPMENT_PENDING`
-   `SHIPPED`
-   `IN_TRANSIT`
-   `DELIVERED`
-   `AWAITING_BUYER_CONFIRMATION`
-   `COMPLETED`
-   `REJECTED`
-   `CANCELLED`
-   `REVIEW_REQUIRED`
-   `REFUND_PENDING`
-   `REFUNDED`

Define valid transitions centrally and enforce them in both backend
workflow logic and the smart contract wherever financial or
authorization state is involved.

### Normalized carrier statuses

Map provider-specific statuses to a common set:

-   `CREATED`
-   `ACCEPTED`
-   `PICKED_UP`
-   `IN_TRANSIT`
-   `OUT_FOR_DELIVERY`
-   `DELIVERED`
-   `DELIVERY_FAILED`
-   `RETURNED`
-   `LOST`
-   `CANCELLED`

A tracking number being created is not proof that a parcel has been
picked up.

------------------------------------------------------------------------

## 6. Logistics Partner Onboarding

Logistics providers must register and be approved before they can be
selected for new transactions.

### Registration information

Collect:

-   Company name.
-   Business registration number.
-   Official email.
-   Website, if available.
-   Business address.
-   Contact person and phone.
-   Operating locations and delivery coverage.
-   Services offered.
-   Verification documents.
-   Tracking API endpoint and authentication configuration, when
    applicable.
-   Settlement wallet address.
-   Pricing information.

### Partner approval states

-   `PENDING`
-   `UNDER_REVIEW`
-   `APPROVED`
-   `INTEGRATION_PENDING`
-   `ACTIVE`
-   `SUSPENDED`
-   `REJECTED`

Only `ACTIVE` partners may appear in the selectable provider directory,
accept new shipments, submit shipment events, or participate in new
transactions.

Suspending a provider blocks new assignments. Existing funded
transactions must continue under their agreed terms or follow a
controlled cancellation/migration process.

### Partner portal

Provide:

-   Business profile.
-   Verification status.
-   Coverage and services.
-   Pricing information.
-   API configuration.
-   Incoming shipment requests.
-   Active shipments.
-   Completed shipments.
-   Failed deliveries.
-   Settlement history.

### Partner API security

-   Use revocable API credentials.
-   Store only hashed API keys.
-   Support key rotation.
-   Rate-limit requests.
-   Restrict each partner to its own assigned shipments.
-   Validate every submitted event.
-   Authenticate webhooks and protect against replay if webhooks are
    implemented.

### Suggested partner endpoints

Implement only endpoints required by the MVP:

``` text
POST   /api/v1/partners/register/
GET    /api/v1/partners/profile/
PATCH  /api/v1/partners/profile/
GET    /api/v1/partners/shipments/
GET    /api/v1/partners/shipments/{id}/
POST   /api/v1/partners/shipments/{id}/accept/
POST   /api/v1/partners/shipments/{id}/events/
GET    /api/v1/partners/settlements/
```

------------------------------------------------------------------------

## 7. Logistics Integration

Use a provider adapter interface so the platform is not coupled to one
carrier.

The common interface should support:

-   Get delivery quote.
-   Create shipment.
-   Retrieve tracking information.
-   Retrieve tracking events.
-   Normalize provider-specific responses.
-   Handle provider errors and unavailable services.

Suggested structure:

``` text
integrations/carriers/
    base.py
    demo_carrier.py
    registry.py
```

### MVP carrier

Implement a `DemoCarrier` adapter that can simulate:

-   Shipment creation.
-   Provider acceptance.
-   Pickup.
-   In-transit updates.
-   Out-for-delivery.
-   Delivery.
-   Failed delivery or return.

Clearly mark every simulated event as demo data in the interface,
database, API responses, and demo presentation. Never present simulated
tracking as a genuine carrier integration or genuine OutLayer
attestation.

### Future real integrations

Real providers such as GIG Logistics, DHL, or FedEx may be added only
after API access and integration requirements are confirmed. Do not
assume public API access or invent endpoint formats.

For the MVP, choose one tracking update mechanism for Demo Carrier.
Polling is a straightforward starting point. Webhooks can be added when
there is a real provider that supports them.

------------------------------------------------------------------------

## 8. Settlement and Fee Model

### Allocation model

The buyer funds the full amount once. The escrow contract records the
agreed allocations.

``` text
total_funded = seller_amount + logistics_amount + platform_fee
```

Example for explaining the model only:

  Allocation                             Example
  ----------------------------------- ----------
  Product price / seller allocation     ₦850,000
  Logistics allocation                   ₦10,000
  VeriPay platform fee                    ₦5,000
  Total                                 ₦865,000

This example is not a promise that testnet token units equal naira. In
the MVP, display the actual supported token and its denomination
accurately.

Network gas is separate from the transaction allocations and should be
disclosed.

### Settlement milestones

**Logistics provider** - Logistics payment becomes eligible at the
agreed dispatch milestone, preferably verified carrier pickup. - Do not
pay merely because a tracking number was generated. - If pickup never
occurs, the logistics allocation should remain unpaid and follow the
cancellation/refund rules.

**Seller** - Seller allocation becomes eligible after delivery
verification and explicit buyer confirmation. - If the buyer reports a
problem, the seller allocation remains locked and the transaction moves
to `REVIEW_REQUIRED`. - Do not auto-release merely because the buyer is
silent.

**VeriPay** - Platform fee must be disclosed before acceptance and
funding. - Use one simple fee model for the MVP: fixed, percentage, or
hybrid. - Collect the fee according to the stated successful-transaction
policy. - Fee configuration changes apply only to new agreements, not
already funded transactions.

### Payout destinations

-   Record beneficiaries before funding.
-   The contract must enforce the recorded destinations.
-   A normal backend request must not be able to change a beneficiary
    after funding.
-   Never let an ordinary backend endpoint arbitrarily release funds.

### Failure cases

-   **Seller does not dispatch:** buyer may request cancellation/refund
    after the agreed deadline; logistics is not paid if the dispatch
    milestone was not reached.
-   **Carrier reports loss:** move to `REVIEW_REQUIRED`; do not
    automatically assume seller fault.
-   **Wrong or damaged goods:** move to `REVIEW_REQUIRED`; keep the
    seller allocation locked. The MVP records the issue but does not
    provide full arbitration.
-   **Delivery verification unavailable:** do not fabricate a positive
    result; keep funds locked or use an explicitly defined manual review
    path.
-   **Duplicate event or payout:** reject through idempotency and
    contract state checks.

### Token and payout boundary

MVP payouts are on-chain transfers to registered NEAR-compatible wallets
using the supported token. Fiat payout to a bank account requires a
separate provider, compliance work, and reconciliation and is outside
the MVP.

------------------------------------------------------------------------

## 9. NEAR Smart Contract

The Rust NEAR smart contract is the financial and authorization source
of truth.

Django may coordinate workflows and maintain a searchable projection,
but it must not be able to bypass contract rules.

### Contract responsibilities

-   Create escrow.
-   Enforce buyer and seller authorization.
-   Accept or reject agreement.
-   Receive and account for buyer funding.
-   Record logistics provider and shipment reference.
-   Record authorized delivery verification.
-   Record buyer confirmation or problem report.
-   Settle logistics allocation at the agreed milestone.
-   Release seller allocation when conditions are satisfied.
-   Collect platform fee.
-   Handle permitted cancellation and refund paths.
-   Prevent duplicate funding and settlement.

### NEP-141 funding

Use the correct NEP-141 fungible-token flow, including
`ft_transfer_call` and the token receiver method required by the
contract design.

Do not treat native NEAR transfers and NEP-141 fungible-token deposits
as interchangeable.

The contract must validate:

-   Supported token contract.
-   Correct escrow ID.
-   Authorized buyer.
-   Expected amount.
-   Current escrow state.
-   No prior successful funding.
-   No duplicate deposit.

### Suggested contract methods

Names are conceptual and must be adapted to the actual Rust/NEAR SDK
version and implementation:

``` text
create_escrow
accept_escrow
reject_escrow
fund_escrow / ft_on_transfer
assign_logistics_provider
register_shipment
record_delivery_verification
confirm_receipt
report_delivery_problem
settle_logistics
release_seller_payment
collect_platform_fee
refund_buyer
cancel_escrow
```

Combine methods where appropriate. Do not mirror every Django endpoint
as a contract method.

### Authorization rules

  -----------------------------------------------------------------------
  Action                              Authorized party
  ----------------------------------- -----------------------------------
  Create agreement                    Seller

  Accept agreement                    Assigned buyer, or first valid
                                      invitee if buyer was not
                                      preassigned

  Fund escrow                         Authorized buyer

  Assign logistics provider           Seller, subject to agreement terms

  Register shipment                   Authorized seller/provider

  Submit delivery verification        Authorized verification source

  Confirm receipt                     Buyer

  Report problem                      Buyer

  Receive seller allocation           Recorded seller beneficiary

  Receive logistics allocation        Recorded logistics beneficiary

  Receive platform fee                Configured platform treasury
  -----------------------------------------------------------------------

### Contract security

-   Validate every state transition.
-   Enforce all permissions on-chain.
-   Prevent replay and duplicate verification.
-   Reject verification for the wrong escrow or shipment.
-   Reject verification after cancellation or final settlement.
-   Prevent double settlement and double fee collection.
-   Keep beneficiary addresses immutable after funding unless an
    explicitly safe and agreed contract process exists.
-   Never store private keys in the contract or backend.
-   Never rely solely on frontend checks.

------------------------------------------------------------------------

## 10. OutLayer Delivery Verification

### Purpose

OutLayer runs verification code in a Trusted Execution Environment
(TEE). The code retrieves tracking data from the carrier source, checks
it against the expected shipment, and produces an attested result.

OutLayer attests to the execution and result of the code. It does not
prove the physical contents or condition of the parcel.

### Verification logic

The verification program should check:

1.  Escrow ID is valid.
2.  Shipment ID belongs to the escrow.
3.  Tracking number matches the registered shipment.
4.  Provider identity matches the assigned provider.
5.  Carrier response is valid and authenticated as required by the
    carrier.
6.  Reported status is `DELIVERED`.
7.  Event timestamp is plausible and consistent with the shipment
    timeline.
8.  The result has not already been used.
9.  The escrow has not been cancelled or completed.

Conceptual result only:

``` json
{
  "escrow_id": "VP-001",
  "shipment_id": "SHIP-001",
  "tracking_number": "DEMO-001",
  "provider_id": "LOG-001",
  "reported_status": "DELIVERED",
  "verified": true,
  "verified_at": "2026-11-13T14:30:00Z"
}
```

The actual payload, attestation format, SDK calls, and contract
submission path must follow current official OutLayer documentation. Do
not invent SDK functions or claim that an ordinary Django request is a
cryptographic attestation.

### Integration requirements

-   Read the current OutLayer documentation before implementation.
-   Build a genuine OutLayer adapter.
-   Keep a separate local mock adapter for development and tests.
-   Never label mock results as genuine TEE attestations.
-   Establish how the attestation is verified and how the authorized
    result reaches the NEAR contract.
-   The contract must accept delivery verification only through the
    authorized verification mechanism.

If the full OutLayer integration cannot be completed, isolate the
interface and clearly disclose the limitation in the demo and README. Do
not fake attestations.

------------------------------------------------------------------------

## 11. Django Application Architecture

Use a **modular monolith**. Do not introduce microservices, FastAPI, or
a separate React/Next.js frontend for the MVP.

### Technology choices

-   Python and Django.
-   Django Templates.
-   HTMX for partial page updates.
-   Alpine.js for lightweight frontend interactions.
-   Tailwind CSS for styling.
-   PostgreSQL for production.
-   SQLite is acceptable for local development.
-   Rust for the NEAR smart contract.

Do not add Redis, Celery, Docker, or additional infrastructure unless a
concrete implementation need justifies it.

### Suggested project structure

``` text
veripay/
├── config/
│   └── settings/
│       ├── base.py
│       ├── development.py
│       └── production.py
├── apps/
│   ├── users/
│   ├── wallets/
│   ├── agreements/
│   ├── escrow/
│   ├── partners/
│   ├── logistics/
│   ├── shipments/
│   ├── verification/
│   ├── settlements/
│   ├── notifications/
│   └── dashboard/
├── integrations/
│   ├── near/
│   │   ├── client.py
│   │   ├── signer.py
│   │   └── events.py
│   ├── outlayer/
│   │   ├── client.py
│   │   └── verifier.py
│   ├── carriers/
│   │   ├── base.py
│   │   ├── demo_carrier.py
│   │   └── registry.py
│   └── payments/
│       ├── base.py
│       └── near_ft.py
├── contracts/
│   └── veripay/
├── templates/
├── static/
├── tests/
├── docs/
├── manage.py
└── README.md
```

This is a guide, not a requirement to create empty apps for every
concept. Combine closely related modules if that keeps the MVP simpler
while preserving clear boundaries.

### Suggested Django models

Create only models needed by implemented flows. Likely candidates:

-   `WalletIdentity`
-   `WalletChallenge`
-   `EscrowAgreement`
-   `EscrowParticipant`
-   `EscrowInvitation`
-   `LogisticsPartner`
-   `PartnerVerificationDocument`
-   `PartnerApiCredential`
-   `LogisticsService`
-   `DeliveryQuote`
-   `Shipment`
-   `ShipmentEvent`
-   `VerificationRequest`
-   `VerificationResult`
-   `SettlementAllocation`
-   `SettlementRecord`
-   `ContractEvent`
-   `Notification`

### Django responsibilities

-   Wallet challenge creation and verification.
-   User sessions and wallet-linked identity.
-   Agreement metadata and invitation links.
-   Partner registration, review, and approval.
-   Shipment coordination.
-   Carrier adapter integration and tracking synchronization.
-   OutLayer integration orchestration.
-   Dashboard and transaction history.
-   Contract event indexing and reconciliation.
-   Notifications and audit logs.
-   Admin operations.

### Source-of-truth rule

-   Smart contract: financial state, authorization, funding, and
    settlement.
-   Django database: metadata, searchable indexes, operational workflow,
    and a projection of on-chain state.
-   Carrier: source of carrier tracking information.
-   OutLayer: attested execution result for the configured verification
    code and its inputs/outputs.

Django must reconcile its local projection with contract state and must
not treat its own database as authoritative for funds.

------------------------------------------------------------------------

## 12. API Surface

Implement only the endpoints required for the end-to-end MVP.

### Wallet

``` text
POST /api/v1/wallet/challenge/
POST /api/v1/wallet/verify/
GET  /api/v1/wallet/me/
```

### Escrow

``` text
POST /api/v1/escrows/
GET  /api/v1/escrows/
GET  /api/v1/escrows/{id}/
POST /api/v1/escrows/{id}/accept/
POST /api/v1/escrows/{id}/reject/
POST /api/v1/escrows/{id}/funding-intent/
POST /api/v1/escrows/{id}/confirm-receipt/
POST /api/v1/escrows/{id}/report-problem/
```

### Invitations

``` text
POST /api/v1/invitations/
GET  /api/v1/invitations/{token}/
POST /api/v1/invitations/{token}/accept/
POST /api/v1/invitations/{token}/revoke/
```

Invitation tokens must be high-entropy, unguessable, stored safely, and
revocable. Possession of the token alone must not authorize contract
actions.

### Logistics and shipments

``` text
GET  /api/v1/logistics/providers/
GET  /api/v1/logistics/providers/{id}/
POST /api/v1/logistics/quotes/
POST /api/v1/shipments/
GET  /api/v1/shipments/{id}/
GET  /api/v1/shipments/{id}/events/
```

### Partner

``` text
POST /api/v1/partners/register/
GET  /api/v1/partners/profile/
PATCH /api/v1/partners/profile/
GET  /api/v1/partners/shipments/
POST /api/v1/partners/shipments/{id}/events/
GET  /api/v1/partners/settlements/
```

------------------------------------------------------------------------

## 13. Frontend Requirements

### Public pages

-   Landing page.
-   How it works.
-   Become a logistics partner.
-   Logistics provider directory.
-   Supported assets.
-   Documentation.
-   Connect wallet.

### User dashboard

-   Overview.
-   Buying.
-   Selling.
-   Pending invitations.
-   Active shipments.
-   Completed transactions.
-   Payment history.
-   Wallet information.

### Escrow detail page

Display:

-   Escrow ID.
-   Buyer and seller wallet addresses.
-   Product information.
-   Funding amount and token.
-   Seller, logistics, and platform allocations.
-   Logistics provider.
-   Tracking number.
-   Shipment timeline.
-   Verification status.
-   Current escrow state.
-   Available actions for the connected user's role.
-   Relevant transaction hashes and explorer links.

### Partner portal

-   Registration and business verification.
-   Profile and coverage.
-   Pricing and services.
-   API configuration.
-   Shipment requests.
-   Active shipments and tracking updates.
-   Settlement history.

### Admin interface

-   Partner applications.
-   Approved, suspended, and rejected partners.
-   Escrow activity.
-   Shipment monitoring.
-   Verification failures.
-   Settlement records.
-   Platform fee configuration.

Make clear distinctions between on-chain confirmed facts,
carrier-reported tracking events, and demo/simulated data.

------------------------------------------------------------------------

## 14. Security and Reliability Requirements

-   Verify wallet signatures on the backend.
-   Use single-use, expiring wallet challenges.
-   Enforce authorization in Django and independently in the contract.
-   Use CSRF protection and server-side input validation.
-   Hash partner API keys and support revocation/rotation.
-   Rate-limit sensitive endpoints.
-   Authenticate webhooks and prevent replay.
-   Use high-entropy invitation tokens.
-   Use idempotency for funding-related orchestration, event processing,
    and settlement operations.
-   Keep secrets in environment variables or a proper secret manager.
-   Never store user private keys or seed phrases.
-   Never trust a frontend-provided wallet address or role.
-   Never allow Django to arbitrarily release escrow funds.
-   Never let carrier tracking status alone trigger seller payout.
-   Never silently alter funded agreement terms or beneficiaries.
-   Keep audit logs for sensitive operations.
-   Handle duplicate, delayed, and out-of-order carrier events.
-   Reconcile local state with on-chain contract state.
-   Fail closed when verification is missing, invalid, or ambiguous.

------------------------------------------------------------------------

## 15. Implementation Phases

Build incrementally. Test each phase before moving to the next.

### Phase 1 --- Repository and foundation

-   Inspect the existing repository and development environment.
-   Confirm toolchain versions.
-   Read current official NEAR and OutLayer documentation.
-   Set up Django and environment-specific settings.
-   Establish the minimal project structure.
-   Add tests and formatting/linting conventions.
-   Write architecture and lifecycle notes.

### Phase 2 --- Smart contract first

-   Implement Rust NEAR contract.
-   Implement create and accept flows.
-   Implement NEP-141 funding.
-   Implement authorization and state transitions.
-   Record explicit allocations.
-   Implement buyer confirmation, settlement, and refund paths.
-   Add unit and integration tests.
-   Deploy to the appropriate NEAR testnet.
-   Verify behavior using real testnet transactions.

### Phase 3 --- Wallet authentication and escrow workflow

-   Implement wallet challenge and signature verification.
-   Create wallet identity records.
-   Implement agreement creation.
-   Implement optional preassigned buyer wallet.
-   Implement invitation links and acceptance.
-   Build Buying and Selling dashboards.
-   Connect frontend actions to wallet-authorized contract calls.

### Phase 4 --- Logistics partner workflow

-   Implement partner registration.
-   Implement admin review and approval.
-   Implement ACTIVE-only provider directory.
-   Build partner portal.
-   Implement partner authentication and shipment access controls.

### Phase 5 --- Shipment and tracking

-   Implement carrier adapter interface.
-   Implement Demo Carrier.
-   Create shipment workflow.
-   Register tracking references.
-   Display shipment events and timeline.
-   Test duplicate, delayed, and out-of-order events.
-   Clearly mark demo events.

### Phase 6 --- OutLayer verification

-   Confirm current OutLayer SDK, supported execution model, attestation
    format, and NEAR submission mechanism from official documentation.
-   Implement the real verification adapter.
-   Implement carrier tracking lookup inside the configured execution
    environment.
-   Verify shipment association and delivery status.
-   Verify attestation through the supported mechanism.
-   Submit the authorized result to the contract.
-   Test invalid, replayed, and mismatched results.
-   Keep mock verification separate and visibly labelled.

### Phase 7 --- Settlement

-   Implement logistics settlement at the agreed dispatch milestone.
-   Implement seller settlement after verified delivery and buyer
    confirmation.
-   Implement platform fee collection.
-   Implement refund and failure paths.
-   Ensure idempotency and no double payouts.
-   Reconcile local settlement records with contract state.

### Phase 8 --- Finalization

-   Run end-to-end tests.
-   Test the complete buyer and seller journeys.
-   Polish responsive UI.
-   Add explorer links and transaction status.
-   Complete README and technical documentation.
-   Record a clear demo video.
-   Confirm the public repository contains setup instructions and no
    secrets.

------------------------------------------------------------------------

## 16. End-to-End Acceptance Criteria

A successful MVP demonstration should show:

1.  Seller connects a NEAR wallet.
2.  Seller creates an escrow agreement.
3.  Seller generates an invitation link.
4.  Buyer opens the invitation and connects a wallet.
5.  Buyer reviews and accepts the agreement.
6.  Buyer funds the actual NEAR escrow contract with the supported
    testnet token.
7.  Buyer and seller dashboards show the same escrow.
8.  Seller selects an approved logistics provider.
9.  Provider accepts the shipment.
10. A tracking reference is registered.
11. Tracking events update the shipment timeline.
12. OutLayer executes the delivery verification flow, if genuine
    integration is completed.
13. The authorized verification result is recorded on-chain.
14. Buyer confirms receipt.
15. Contract pays the seller allocation.
16. Contract settles the logistics allocation according to its agreed
    milestone.
17. Contract pays the platform fee.
18. The transaction and settlement records can be inspected on the NEAR
    testnet explorer.

If any step is simulated, explicitly identify it as simulated. Never
fabricate a carrier integration, testnet transaction, or OutLayer
attestation.

------------------------------------------------------------------------

## 17. Required Documentation

Maintain these documents as implementation progresses:

``` text
README.md
docs/
    architecture.md
    escrow-lifecycle.md
    settlement-rules.md
    logistics-partner-api.md
    outlayer-verification.md
    smart-contract.md
    development-setup.md
```

The README must explain:

-   What VeriPay does.
-   What is and is not implemented.
-   Architecture overview.
-   Supported network and token.
-   Local development setup.
-   Environment variables required, without exposing values.
-   Smart contract build and deployment steps.
-   How to run tests.
-   Demo workflow.
-   Which components are simulated.
-   Known limitations and future roadmap.

------------------------------------------------------------------------

## 18. Instructions to the Coding Agent

Before writing substantial code:

1.  Inspect the repository, existing files, and current environment.
2.  Identify the available NEAR toolchain and Rust target.
3.  Read current official NEAR documentation relevant to the selected
    SDK and NEP-141.
4.  Read current official OutLayer documentation and confirm the actual
    integration path.
5.  Propose a concise implementation plan.
6.  Identify risks, especially NEP-141 funding, wallet authorization,
    OutLayer attestation verification, and contract settlement.
7.  Propose a practical project structure that avoids unnecessary
    complexity.
8.  Wait for approval before large architectural changes or destructive
    operations.

While implementing:

-   Work in small, testable increments.
-   Do not generate the entire application in one pass.
-   Do not invent SDK methods, API endpoints, or attestation formats.
-   Do not add AI or NEAR Intents to the MVP.
-   Do not introduce React, FastAPI, microservices, or infrastructure
    without a concrete need.
-   Do not delete or rewrite existing files without inspecting them
    first.
-   Run relevant tests after each meaningful change.
-   Keep financial state changes protected by authorization, state
    validation, idempotency, and tests.
-   Never place private keys or secrets in source control.
-   Clearly distinguish real integrations from mocks and simulations.
-   Update documentation as implementation decisions are made.

### Primary engineering principle

**The NEAR smart contract controls funds and enforces transaction
permissions. Django coordinates the product experience. Carrier systems
report shipment events. OutLayer attests to the execution of the
configured verification logic. Buyer confirmation remains a separate
condition for seller settlement.**

Build the smallest complete, verifiable end-to-end workflow before
adding optional features.
