//! VeriPay escrow contract (NEAR testnet).
//!
//! Financial and authorization source of truth for VeriPay agreements:
//! seller creates -> buyer accepts -> buyer funds via NEP-141
//! (`ft_transfer_call`) -> seller assigns a logistics provider -> shipment
//! registered -> OutLayer TEE verifies carrier-reported delivery -> buyer
//! confirms receipt -> allocations settle (seller, logistics, platform fee).
//!
//! Boundaries (mirrors the project specification):
//! - Delivery verification is accepted ONLY through the OutLayer callback
//!   (`on_verification_result`), which is `#[private]` and therefore only
//!   reachable from this contract's own promise chain back from
//!   `outlayer.testnet`. Nothing else may set the verification flags.
//! - Beneficiaries are fixed at creation/funding time; no method changes them.
//! - Carrier tracking status alone never releases the seller allocation.

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::json_types::U128;
use near_sdk::serde::{Deserialize, Serialize};
use near_sdk::serde_json::json;
use near_sdk::store::LookupMap;
use near_sdk::{env, log, near, require, AccountId, BorshStorageKey, Gas, NearToken, PanicOnDefault, Promise};


const MIN_VERIFICATION_DEPOSIT_YOCTO: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR
const OUTLAYER_GAS_TGAS: u64 = 100;
const CALLBACK_GAS_TGAS: u64 = 20;

#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, PartialEq, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub enum EscrowState {
    PendingAcceptance,
    Approved,
    Funded,
    ShipmentPending,
    Shipped,
    AwaitingBuyerConfirmation,
    Completed,
    Rejected,
    Cancelled,
    ReviewRequired,
    Refunded,
}

/// What the OutLayer verification module returns (its JSON stdout).
#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct VerificationOutcome {
    pub escrow_id: String,
    pub tracking_number: String,
    pub provider_id: String,
    pub reported_status: String,
    pub verified: bool,
    pub reason: String,
}

/// On-chain trust record for a wallet. Counters move only inside existing
/// state transitions — never on creation, and disputes stay neutral until
/// resolved. `volume` is the sum of seller amounts over completed deals.
#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct Reputation {
    pub completed_as_seller: u32,
    pub completed_as_buyer: u32,
    pub disputes_opened: u32,
    pub refunds_claimed: u32,
    pub requests_rejected: u32,
    pub volume: U128,
    pub first_seen_ms: u64,
}

impl Default for Reputation {
    fn default() -> Self {
        Self {
            completed_as_seller: 0,
            completed_as_buyer: 0,
            disputes_opened: 0,
            refunds_claimed: 0,
            requests_rejected: 0,
            volume: U128(0),
            first_seen_ms: 0,
        }
    }
}

#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct Escrow {
    pub id: String,
    pub seller: AccountId,
    pub buyer: Option<AccountId>,
    pub invite_hash: Option<String>,
    pub state: EscrowState,
    pub product_name: String,
    pub product_desc_hash: String,
    pub seller_amount: U128,
    pub logistics_amount: U128,
    pub platform_fee: U128,
    pub total_amount: U128,
    pub token: AccountId,
    pub logistics_provider: Option<AccountId>,
    pub shipment_id: String,
    pub tracking_number: Option<String>,
    pub dispatch_deadline_ms: u64,
    pub inspection_period_ms: u64,
    pub created_ms: u64,
    pub terms_hash: String,
    pub shipment_registered: bool,
    pub verification_pending: bool,
    pub pickup_verified: bool,
    pub delivered_verified: bool,
    pub problem_reported: bool,
    pub logistics_paid: bool,
    pub seller_paid: bool,
    pub platform_paid: bool,
    pub last_verification: Option<VerificationOutcome>,
}

#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct ConfigView {
    pub owner: AccountId,
    pub outlayer_contract: AccountId,
    pub outlayer_project: String,
    pub platform_fee_bps: u16,
}

#[derive(BorshSerialize, BorshStorageKey)]
#[borsh(crate = "near_sdk::borsh")]
enum StorageKey {
    Escrows,
    BuyerIndex,
    SellerIndex,
    PendingIndex,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct VeriPay {
    owner: AccountId,
    outlayer_contract: AccountId,
    outlayer_project: String,
    platform_fee_bps: u16,
    escrows: LookupMap<String, Escrow>,
    buyer_index: LookupMap<AccountId, Vec<String>>,
    seller_index: LookupMap<AccountId, Vec<String>>,
    pending_index: LookupMap<AccountId, Vec<String>>,
    next_id: u64,
}

fn sha256_hex(input: &[u8]) -> String {
    env::sha256(input).iter().map(|b| format!("{:02x}", b)).collect()
}

fn push_index(map: &mut LookupMap<AccountId, Vec<String>>, key: &AccountId, id: &str) {
    let mut v = map.get(key).cloned().unwrap_or_default();
    if !v.iter().any(|x| x == id) {
        v.push(id.to_string());
    }
    map.insert(key.clone(), v);
}

#[near]
impl VeriPay {
    #[init]
    pub fn new(owner: AccountId, outlayer_contract: AccountId, outlayer_project: String, platform_fee_bps: u16) -> Self {
        require!(platform_fee_bps <= 2_000, "fee bps too high");
        Self {
            owner,
            outlayer_contract,
            outlayer_project,
            platform_fee_bps,
            escrows: LookupMap::new(StorageKey::Escrows),
            buyer_index: LookupMap::new(StorageKey::BuyerIndex),
            seller_index: LookupMap::new(StorageKey::SellerIndex),
            pending_index: LookupMap::new(StorageKey::PendingIndex),
            next_id: 1,
        }
    }

    // ------------------------------------------------------------------
    // Agreement lifecycle
    // ------------------------------------------------------------------

    /// Seller creates the agreement. `buyer` may be preassigned (Case A) or
    /// left empty with an `invite_hash` (Case B): sha256 hex of a
    /// high-entropy invitation secret that the buyer must present on accept.
    /// The platform fee is enforced from the configured bps.
    #[payable]
    pub fn create_escrow(
        &mut self,
        product_name: String,
        product_desc_hash: String,
        seller_amount: U128,
        logistics_amount: U128,
        buyer: Option<AccountId>,
        invite_hash: Option<String>,
        token: AccountId,
        dispatch_deadline_ms: u64,
        inspection_period_ms: u64,
        terms_hash: String,
    ) -> String {
        let seller = env::predecessor_account_id();
        require!(seller_amount.0 > 0, "seller amount must be > 0");
        if let Some(b) = &buyer {
            require!(b != &seller, "buyer must differ from seller");
        } else {
            require!(invite_hash.is_some(), "invite_hash required when buyer is not preassigned");
        }
        require!(dispatch_deadline_ms > env::block_timestamp() / 1_000_000, "dispatch deadline in the past");

        let platform_fee = seller_amount.0 * self.platform_fee_bps as u128 / 10_000;
        let total_amount = seller_amount.0 + logistics_amount.0 + platform_fee;

        let id = format!("VP-{:06}", self.next_id);
        self.next_id += 1;

        let escrow = Escrow {
            id: id.clone(),
            seller: seller.clone(),
            buyer: buyer.clone(),
            invite_hash,
            state: EscrowState::PendingAcceptance,
            product_name,
            product_desc_hash,
            seller_amount,
            logistics_amount,
            platform_fee: U128(platform_fee),
            total_amount: U128(total_amount),
            token,
            logistics_provider: None,
            shipment_id: format!("SHIP-{}", id),
            tracking_number: None,
            dispatch_deadline_ms,
            inspection_period_ms,
            created_ms: env::block_timestamp() / 1_000_000,
            terms_hash,
            shipment_registered: false,
            verification_pending: false,
            pickup_verified: false,
            delivered_verified: false,
            problem_reported: false,
            logistics_paid: false,
            seller_paid: false,
            platform_paid: false,
            last_verification: None,
        };
        self.escrows.insert(id.clone(), escrow);
        push_index(&mut self.seller_index, &seller, &id);
        bump_reputation(&seller, |_| {});
        if let Some(b) = buyer {
            push_index(&mut self.pending_index, &b, &id);
        }
        log!("created escrow {} for seller {}", id, seller);
        id
    }

    /// Buyer accepts the agreement. Case A: only the preassigned wallet.
    /// Case B: the first wallet presenting the invitation secret becomes the
    /// buyer (possession of the link alone is not authorization; the signed
    /// transaction is).
    pub fn accept_escrow(&mut self, escrow_id: String, invite_secret: Option<String>) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::PendingAcceptance, "wrong state");
        let caller = env::predecessor_account_id();
        match &escrow.buyer {
            Some(b) => require!(&caller == b, "only the preassigned buyer can accept"),
            None => {
                let secret = invite_secret.expect("invite secret required");
                let hash = sha256_hex(secret.as_bytes());
                require!(Some(&hash) == escrow.invite_hash.as_ref(), "invalid invite secret");
                require!(caller != escrow.seller, "seller cannot be the buyer");
                escrow.buyer = Some(caller.clone());
            }
        }
        escrow.state = EscrowState::Approved;
        self.escrows.insert(escrow_id.clone(), escrow);
        push_index(&mut self.buyer_index, &caller, &escrow_id);
        pop_index(&mut self.pending_index, &caller, &escrow_id);
        bump_reputation(&caller, |_| {});
        log!("escrow {} approved by buyer {}", escrow_id, caller);
    }

    /// Buyer declines the agreement.
    pub fn reject_escrow(&mut self, escrow_id: String) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::PendingAcceptance, "wrong state");
        let caller = env::predecessor_account_id();
        require!(Some(&caller) == escrow.buyer.as_ref(), "only the buyer can reject");
        escrow.state = EscrowState::Rejected;
        self.escrows.insert(escrow_id.clone(), escrow);
        pop_index(&mut self.pending_index, &caller, &escrow_id);
        bump_reputation(&caller, |r| r.requests_rejected += 1);
    }

    /// Seller cancels while still pending acceptance.
    pub fn cancel_escrow(&mut self, escrow_id: String) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::PendingAcceptance, "wrong state");
        require!(env::predecessor_account_id() == escrow.seller, "only the seller can cancel");
        escrow.state = EscrowState::Cancelled;
        let buyer = escrow.buyer.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        if let Some(b) = buyer {
            pop_index(&mut self.pending_index, &b, &escrow_id);
        }
    }

    // ------------------------------------------------------------------
    // NEP-141 funding
    // ------------------------------------------------------------------

    /// NEP-141 receiver. `msg` is the escrow id. Validates the token contract,
    /// the buyer, the exact total and the state; anything else is refunded by
    /// returning the amount (the FT contract refunds it to the sender).
    #[payable]
    pub fn ft_on_transfer(&mut self, sender_id: AccountId, amount: U128, msg: String) -> String {
        let escrow_id = msg.trim().to_string();
        let mut escrow = match self.escrows.get(&escrow_id) {
            Some(e) => e.clone(),
            None => return amount.0.to_string(),
        };
        let token = env::predecessor_account_id();
        if token != escrow.token
            || escrow.state != EscrowState::Approved
            || Some(&sender_id) != escrow.buyer.as_ref()
            || amount.0 != escrow.total_amount.0
        {
            log!("ft_on_transfer rejected: token/state/buyer/amount mismatch for {}", escrow_id);
            return amount.0.to_string();
        }
        escrow.state = EscrowState::Funded;
        self.escrows.insert(escrow_id.clone(), escrow);
        log!("escrow {} funded with {} by {}", escrow_id, amount.0, sender_id);
        "0".to_string()
    }

    // ------------------------------------------------------------------
    // Logistics and shipment
    // ------------------------------------------------------------------

    /// Seller assigns an approved logistics provider (recorded beneficiary).
    pub fn assign_logistics_provider(&mut self, escrow_id: String, provider: AccountId) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(env::predecessor_account_id() == escrow.seller, "only the seller assigns the provider");
        require!(escrow.state == EscrowState::Funded, "escrow not funded");
        require!(provider != escrow.seller, "provider must differ from seller");
        escrow.logistics_provider = Some(provider);
        escrow.state = EscrowState::ShipmentPending;
        self.escrows.insert(escrow_id, escrow);
    }

    /// Seller or the assigned provider registers the carrier tracking number.
    /// Registering a tracking number is NOT proof of pickup and unlocks no
    /// payment by itself.
    pub fn register_shipment(&mut self, escrow_id: String, tracking_number: String) {
        let mut escrow = self.internal_get(&escrow_id);
        let caller = env::predecessor_account_id();
        require!(
            caller == escrow.seller || Some(&caller) == escrow.logistics_provider.as_ref(),
            "only seller or provider can register the shipment"
        );
        require!(escrow.state == EscrowState::ShipmentPending, "wrong state");
        require!(!tracking_number.trim().is_empty(), "tracking number required");
        escrow.tracking_number = Some(tracking_number);
        escrow.shipment_registered = true;
        escrow.state = EscrowState::Shipped;
        self.escrows.insert(escrow_id, escrow);
    }

    // ------------------------------------------------------------------
    // OutLayer delivery verification (TEE)
    // ------------------------------------------------------------------

    /// Requests TEE verification of the carrier-reported delivery data.
    /// Permissionless among the parties; the carrier payload is supplied by
    /// the caller (the backend passes the Demo Carrier response). Attach the
    /// OutLayer compute deposit; unused funds are refunded to this contract.
    #[payable]
    pub fn request_delivery_verification(&mut self, escrow_id: String, carrier_payload: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        let caller = env::predecessor_account_id();
        require!(
            caller == escrow.seller
                || Some(&caller) == escrow.logistics_provider.as_ref()
                || Some(&caller) == escrow.buyer.as_ref(),
            "not authorized"
        );
        require!(escrow.state == EscrowState::Shipped, "escrow not shipped");
        require!(!escrow.verification_pending, "verification already pending");
        require!(
            env::attached_deposit() >= NearToken::from_yoctonear(MIN_VERIFICATION_DEPOSIT_YOCTO),
            "attach at least 0.001 NEAR for OutLayer compute"
        );

        let carrier: serde_json::Value = serde_json::from_str(&carrier_payload)
            .unwrap_or_else(|_| json!({ "parse_error": true }));

        let input = json!({
            "escrow_id": escrow.id,
            "expected": {
                "tracking_number": escrow.tracking_number,
                "provider_wallet": escrow.logistics_provider,
                "shipment_id": escrow.shipment_id,
            },
            "carrier": carrier,
        })
        .to_string();

        let args = json!({
            "source": { "Project": { "project_id": self.outlayer_project, "version_key": null } },
            "resource_limits": {
                "max_instructions": 2_000_000_000u64,
                "max_memory_mb": 128,
                "max_execution_seconds": 30,
            },
            "input_data": input,
            "response_format": "Json",
        })
        .to_string()
        .into_bytes();

        escrow.verification_pending = true;
        self.escrows.insert(escrow_id.clone(), escrow);

        Promise::new(self.outlayer_contract.clone())
            .function_call(
                "request_execution".to_string(),
                args,
                env::attached_deposit(),
                Gas::from_tgas(OUTLAYER_GAS_TGAS),
            )
            .then(
                Self::ext(env::current_account_id())
                    .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                    .on_verification_result(escrow_id),
            )
    }

    /// OutLayer callback. `#[private]` so only this contract's own promise
    /// chain can reach it — the single authorized delivery-verification path.
    #[private]
    pub fn on_verification_result(
        &mut self,
        escrow_id: String,
        #[callback_result] result: Result<Option<VerificationOutcome>, near_sdk::PromiseError>,
    ) -> Option<VerificationOutcome> {
        let Some(mut escrow) = self.escrows.get(&escrow_id).cloned() else { return None };
        escrow.verification_pending = false;
        match result {
            Ok(Some(outcome)) => {
                if outcome.escrow_id != escrow.id {
                    log!("verification escrow mismatch: {} != {}", outcome.escrow_id, escrow.id);
                    self.escrows.insert(escrow_id, escrow);
                    return None;
                }
                if outcome.verified {
                    match outcome.reported_status.as_str() {
                        "PICKED_UP" | "IN_TRANSIT" | "OUT_FOR_DELIVERY" | "DELIVERED" => {
                            escrow.pickup_verified = true;
                        }
                        _ => {}
                    }
                    if outcome.reported_status == "DELIVERED" && escrow.state == EscrowState::Shipped {
                        escrow.delivered_verified = true;
                        escrow.state = EscrowState::AwaitingBuyerConfirmation;
                    }
                    log!("verified {} status {} for {}", outcome.escrow_id, outcome.reported_status, escrow.id);
                } else {
                    log!("verification failed for {}: {}", escrow.id, outcome.reason);
                }
                escrow.last_verification = Some(outcome.clone());
                self.escrows.insert(escrow_id, escrow);
                Some(outcome)
            }
            Ok(None) => {
                log!("OutLayer run failed or returned nothing for {}", escrow_id);
                self.escrows.insert(escrow_id, escrow);
                None
            }
            Err(e) => {
                log!("OutLayer promise error for {}: {:?}", escrow_id, e);
                self.escrows.insert(escrow_id, escrow);
                None
            }
        }
    }

    // ------------------------------------------------------------------
    // Settlement
    // ------------------------------------------------------------------

    /// Pays the logistics allocation once carrier pickup is TEE-verified.
    /// Callable by seller or provider. A failed FT transfer rolls the flag
    /// back (see `on_payout_result`) so the payout can be retried.
    pub fn settle_logistics(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        let caller = env::predecessor_account_id();
        require!(
            caller == escrow.seller || Some(&caller) == escrow.logistics_provider.as_ref(),
            "only seller or provider settles logistics"
        );
        require!(escrow.pickup_verified, "pickup not verified yet");
        require!(!escrow.logistics_paid, "logistics already paid");
        let provider = escrow.logistics_provider.clone().expect("provider not assigned");
        escrow.logistics_paid = true;
        let amount = escrow.logistics_amount;
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        ft_transfer(token, provider, amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "logistics".to_string()),
        )
    }

    /// Buyer confirms receipt after TEE-verified delivery. Pays the seller
    /// allocation; the platform fee follows in `finish_platform`.
    pub fn confirm_receipt(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer confirms receipt");
        require!(escrow.delivered_verified, "delivery not verified");
        require!(!escrow.problem_reported, "problem reported; resolve review first");
        require!(!escrow.seller_paid, "already settled");
        escrow.state = EscrowState::Completed;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        let seller_amount = escrow.seller_amount;
        let seller = escrow.seller.clone();
        let buyer = escrow.buyer.clone().expect("buyer set");
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        // Trust record: completion counts for both sides; volume is deal value.
        bump_reputation(&seller, |r| {
            r.completed_as_seller += 1;
            r.volume.0 += seller_amount.0;
        });
        bump_reputation(&buyer, |r| {
            r.completed_as_buyer += 1;
            r.volume.0 += seller_amount.0;
        });
        ft_transfer(token, seller, seller_amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_platform(escrow_id),
        )
    }

    /// After the seller transfer settles: pay the platform fee, or roll the
    /// completion flags back so the buyer can retry.
    #[private]
    pub fn finish_platform(&mut self, escrow_id: String) {
        if !payout_ok(0) {
            let mut escrow = self.internal_get(&escrow_id);
            escrow.seller_paid = false;
            escrow.platform_paid = false;
            escrow.state = EscrowState::AwaitingBuyerConfirmation;
            self.escrows.insert(escrow_id, escrow);
            return;
        }
        let escrow = self.internal_get(&escrow_id);
        let promise = ft_transfer(escrow.token.clone(), self.owner.clone(), escrow.platform_fee);
        let _ = promise.then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "platform".to_string()),
        );
    }

    /// Rolls a payout flag back when its FT transfer failed (idempotency guard).
    #[private]
    pub fn on_payout_result(&mut self, escrow_id: String, party: String) {
        if payout_ok(0) {
            return;
        }
        let Some(mut escrow) = self.escrows.get(&escrow_id).cloned() else { return };
        match party.as_str() {
            "logistics" => escrow.logistics_paid = false,
            "platform" => escrow.platform_paid = false,
            "refund" => escrow.state = EscrowState::Funded,
            "review" => {
                escrow.seller_paid = false;
                escrow.platform_paid = false;
                escrow.state = EscrowState::ReviewRequired;
            }
            _ => {}
        }
        log!("payout {} failed for {} — flag rolled back", party, escrow_id);
        self.escrows.insert(escrow_id, escrow);
    }

    /// Buyer reports a problem: funds stay locked, escrow moves to review.
    pub fn report_problem(&mut self, escrow_id: String, reason_hash: String) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer reports a problem");
        require!(
            escrow.state == EscrowState::Shipped || escrow.state == EscrowState::AwaitingBuyerConfirmation,
            "wrong state"
        );
        escrow.problem_reported = true;
        escrow.terms_hash = format!("{}|problem:{}", escrow.terms_hash, reason_hash);
        escrow.state = EscrowState::ReviewRequired;
        self.escrows.insert(escrow_id, escrow);
        bump_reputation(&env::predecessor_account_id(), |r| r.disputes_opened += 1);
    }

    /// Explicit manual review path (platform operator). Not arbitration: it
    /// releases the remaining balance as a simple split between seller and
    /// buyer. Disclosed as a centralized fallback in the README.
    pub fn resolve_review(&mut self, escrow_id: String, seller_pct: u8) -> Promise {
        require!(env::predecessor_account_id() == self.owner, "only the platform owner resolves review");
        require!(seller_pct <= 100, "pct must be 0..=100");
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::ReviewRequired, "wrong state");
        require!(!escrow.seller_paid, "already settled");
        let remaining = escrow.total_amount.0 - if escrow.logistics_paid { escrow.logistics_amount.0 } else { 0 };
        let seller_part = remaining * seller_pct as u128 / 100;
        let buyer_part = remaining - seller_part;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        escrow.state = if buyer_part == 0 { EscrowState::Completed } else { EscrowState::Refunded };
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        ft_transfer(token, seller, U128(seller_part)).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_review_payout(escrow_id, U128(buyer_part)),
        )
    }

    #[private]
    pub fn finish_review_payout(&mut self, escrow_id: String, buyer_part: U128) {
        if !payout_ok(0) {
            self.on_payout_result(escrow_id, "review".to_string());
            return;
        }
        let escrow = self.internal_get(&escrow_id);
        if buyer_part.0 == 0 {
            return;
        }
        let buyer = escrow.buyer.clone().expect("no buyer");
        let promise = ft_transfer(escrow.token.clone(), buyer, buyer_part);
        let _ = promise.then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "review".to_string()),
        );
    }

    /// Buyer refund path: seller never dispatched. Only after the dispatch
    /// deadline and only if no shipment was registered. Logistics forfeits.
    pub fn refund_buyer(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer can claim this refund");
        require!(
            escrow.state == EscrowState::Funded || escrow.state == EscrowState::ShipmentPending,
            "wrong state"
        );
        require!(!escrow.shipment_registered, "shipment already registered");
        require!(
            env::block_timestamp() / 1_000_000 >= escrow.dispatch_deadline_ms,
            "dispatch deadline not reached"
        );
        escrow.state = EscrowState::Refunded;
        let buyer = escrow.buyer.clone().unwrap();
        let amount = escrow.total_amount;
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        bump_reputation(&buyer, |r| r.refunds_claimed += 1);
        ft_transfer(token, buyer, amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "refund".to_string()),
        )
    }

    /// Platform owner sweep for OutLayer refund credits and dust (native NEAR).
    pub fn withdraw(&mut self, amount: U128, to: AccountId) -> Promise {
        require!(env::predecessor_account_id() == self.owner, "only the owner can withdraw");
        Promise::new(to).transfer(NearToken::from_yoctonear(amount.0))
    }

    /// Platform owner sweep for NEP-141 balances (e.g. to recover a stuck
    /// escrow in an explicit operator action, disclosed in the README).
    pub fn withdraw_ft(&mut self, token: AccountId, amount: U128, to: AccountId) -> Promise {
        require!(env::predecessor_account_id() == self.owner, "only the owner can withdraw");
        ft_transfer(token, to, amount)
    }

    pub fn update_platform_fee_bps(&mut self, bps: u16) {
        require!(env::predecessor_account_id() == self.owner, "only the owner can update the fee");
        require!(bps <= 2_000, "fee bps too high");
        self.platform_fee_bps = bps;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    pub fn get_escrow(&self, escrow_id: String) -> Option<Escrow> {
        self.escrows.get(&escrow_id).cloned()
    }

    pub fn escrows_for(&self, account_id: AccountId) -> Vec<Escrow> {
        let mut ids: Vec<String> = self.buyer_index.get(&account_id).cloned().unwrap_or_default();
        ids.extend(self.seller_index.get(&account_id).cloned().unwrap_or_default());
        ids.sort();
        ids.iter().filter_map(|id| self.escrows.get(id).cloned()).collect()
    }

    pub fn pending_for(&self, account_id: AccountId) -> Vec<Escrow> {
        self.pending_index
            .get(&account_id)
            .cloned()
            .unwrap_or_default()
            .iter()
            .filter_map(|id| self.escrows.get(id).cloned())
            .collect()
    }

    // ------------------------------------------------------------------
    // Reputation & discovery
    // ------------------------------------------------------------------

    /// On-chain trust record for a wallet (zeros when never seen).
    pub fn reputation_of(&self, account_id: AccountId) -> Reputation {
        reputation_map().get(&account_id).cloned().unwrap_or_default()
    }

    /// Batch variant for list pages.
    pub fn reputations(&self, account_ids: Vec<AccountId>) -> Vec<Reputation> {
        account_ids.iter().map(|a| self.reputation_of(a.clone())).collect()
    }

    /// Page through every escrow ever created (oldest first), so any indexer
    /// can rebuild the whole book from chain data alone. `limit` caps at 50.
    pub fn escrows_range(&self, start: u64, limit: u64) -> Vec<Escrow> {
        let limit = limit.min(50);
        let mut out = Vec::new();
        let mut i = start.max(1);
        while (out.len() as u64) < limit && i < self.next_id {
            let id = format!("VP-{:06}", i);
            if let Some(e) = self.escrows.get(&id) {
                out.push(e.clone());
            }
            i += 1;
        }
        out
    }

    pub fn escrow_count(&self) -> u64 {
        self.next_id - 1
    }

    pub fn get_config(&self) -> ConfigView {
        ConfigView {
            owner: self.owner.clone(),
            outlayer_contract: self.outlayer_contract.clone(),
            outlayer_project: self.outlayer_project.clone(),
            platform_fee_bps: self.platform_fee_bps,
        }
    }

    // ------------------------------------------------------------------
    // Internal
    // ------------------------------------------------------------------

    fn internal_get(&self, escrow_id: &str) -> Escrow {
        self.escrows.get(&escrow_id.to_string()).cloned().expect("unknown escrow")
    }
}

fn pop_index(map: &mut LookupMap<AccountId, Vec<String>>, key: &AccountId, id: &str) {
    let mut v = map.get(key).cloned().unwrap_or_default();
    v.retain(|x| x != id);
    map.insert(key.clone(), v);
}

/// NEP-141 payout: the escrow contract holds tUSD, so settlements call
/// `ft_transfer` on the token contract (1 yoctoNEAR is required by the FT
/// standard for transfers).
fn ft_transfer(token: AccountId, receiver_id: AccountId, amount: U128) -> Promise {
    let args = serde_json::json!({
        "receiver_id": receiver_id,
        "amount": amount,
        "memo": null,
    })
    .to_string()
    .into_bytes();
    Promise::new(token).function_call(
        "ft_transfer".to_string(),
        args,
        NearToken::from_yoctonear(1),
        Gas::from_tgas(20),
    )
}

/// True when the promise at `index` (a chained callback's input) succeeded.
fn payout_ok(index: u64) -> bool {
    matches!(env::promise_result(index), near_sdk::PromiseResult::Successful(_))
}

// ---------------------------------------------------------------------------
// Reputation storage
//
// Kept OUT of the contract state struct on purpose: Borsh deserialization is
// positional, so adding fields to `VeriPay` would brick existing state on
// upgrade. Collections built locally from a constant prefix share the same
// storage without touching the serialized struct at all.
// ---------------------------------------------------------------------------

fn reputation_map() -> LookupMap<AccountId, Reputation> {
    LookupMap::new(b"r1".to_vec())
}

/// Read-modify-write a wallet's reputation, initialising `first_seen_ms`.
fn bump_reputation(account: &AccountId, f: impl FnOnce(&mut Reputation)) {
    let mut map = reputation_map();
    let mut rep = map.get(account).cloned().unwrap_or_default();
    if rep.first_seen_ms == 0 {
        rep.first_seen_ms = env::block_timestamp() / 1_000_000;
    }
    f(&mut rep);
    map.insert(account.clone(), rep);
}
