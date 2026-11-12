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
const MIN_VOTE_STAKE_YOCTO: u128 = 10_000_000_000_000_000_000_000; // 0.01 NEAR per vote
const MIN_ARBITRATION_STAKE_YOCTO: u128 = 50_000_000_000_000_000_000_000; // 0.05 NEAR quorum
const MAX_ARBITRATION_VOTES: usize = 24;

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

/// One arbitration vote (stake is in yoctoNEAR, refundable after finalization).
#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct ArbitrationVote {
    pub voter: AccountId,
    pub seller_pct: u8,
    pub stake: U128,
    pub voted_ms: u64,
    pub claimed: bool,
}

/// An open arbitration case for an escrow in `ReviewRequired`.
#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct ArbitrationCase {
    pub escrow_id: String,
    pub opened_ms: u64,
    pub votes: Vec<ArbitrationVote>,
    pub total_stake: U128,
    pub finalized: bool,
    pub winning_pct: u8,
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
        logistics_provider: Option<AccountId>,
        tracking_number: Option<String>,
    ) -> String {
        let seller = env::predecessor_account_id();
        require!(seller_amount.0 > 0, "seller amount must be > 0");
        if let Some(b) = &buyer {
            require!(b != &seller, "buyer must differ from seller");
        } else {
            require!(invite_hash.is_some(), "invite_hash required when buyer is not preassigned");
        }
        if let Some(p) = &logistics_provider {
            require!(p != &seller, "provider must differ from seller");
        }
        require!(dispatch_deadline_ms > env::block_timestamp() / 1_000_000, "dispatch deadline in the past");

        let platform_fee = seller_amount.0 * self.platform_fee_bps as u128 / 10_000;
        let total_amount = seller_amount.0 + logistics_amount.0 + platform_fee;

        let id = format!("VP-{:06}", self.next_id);
        self.next_id += 1;

        let tracking_number = tracking_number.filter(|t| !t.trim().is_empty());
        let has_tracking = tracking_number.is_some();
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
            logistics_provider,
            shipment_id: format!("SHIP-{}", id),
            tracking_number,
            dispatch_deadline_ms,
            inspection_period_ms,
            created_ms: env::block_timestamp() / 1_000_000,
            terms_hash,
            shipment_registered: has_tracking,
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
        // An empty tracking number reuses the one committed at creation (the
        // marketplace flow pre-registers it so OutLayer can track from day one).
        let tracking = if tracking_number.trim().is_empty() {
            escrow
                .tracking_number
                .clone()
                .expect("no tracking number registered at creation")
        } else {
            tracking_number
        };
        escrow.tracking_number = Some(tracking);
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
        let promise = ft_transfer(escrow.token.clone(), self.platform_fee_recipient(), escrow.platform_fee);
        let _ = promise.then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "platform".to_string()),
        );
    }

    /// Permissionless retry for a failed platform-fee payout (e.g. the fee
    /// recipient was not registered in the token at completion time, or the
    /// transfer ran out of gas). Funds can only ever go to the configured
    /// fee recipient — safe for anyone to call.
    pub fn retry_platform_payout(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.seller_paid, "seller payout not complete");
        require!(!escrow.platform_paid, "platform fee already paid");
        require!(
            escrow.state == EscrowState::Completed || escrow.state == EscrowState::Refunded,
            "escrow not settled"
        );
        escrow.platform_paid = true;
        let platform_fee = escrow.platform_fee;
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        ft_transfer(token, self.platform_fee_recipient(), platform_fee).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "platform".to_string()),
        )
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

    /// Either party can open a dispute. The buyer may escalate from
    /// shipped/awaiting-confirmation states (same as report_problem); the
    /// seller may additionally escalate from funded/shipment-pending states
    /// (e.g. an unresponsive buyer) — this closes the "funds locked with no
    /// exit" gap. Funds stay locked until the review path resolves them.
    pub fn open_dispute(&mut self, escrow_id: String, reason_hash: String) {
        let mut escrow = self.internal_get(&escrow_id);
        let caller = env::predecessor_account_id();
        let is_buyer = Some(&caller) == escrow.buyer.as_ref();
        let is_seller = caller == escrow.seller;
        require!(is_buyer || is_seller, "only buyer or seller can open a dispute");
        require!(escrow.state != EscrowState::ReviewRequired, "already disputed");
        if is_buyer {
            require!(
                escrow.state == EscrowState::Shipped || escrow.state == EscrowState::AwaitingBuyerConfirmation,
                "buyer cannot dispute in this state"
            );
        } else {
            require!(
                escrow.state == EscrowState::Funded
                    || escrow.state == EscrowState::ShipmentPending
                    || escrow.state == EscrowState::Shipped
                    || escrow.state == EscrowState::AwaitingBuyerConfirmation,
                "seller cannot dispute in this state"
            );
        }
        let side = if is_buyer { "buyer" } else { "seller" };
        escrow.problem_reported = true;
        escrow.terms_hash = format!("{}|dispute:{}:{}", escrow.terms_hash, side, reason_hash);
        escrow.state = EscrowState::ReviewRequired;
        log!("dispute opened by {} on {}", side, escrow_id);
        self.escrows.insert(escrow_id, escrow);
        bump_reputation(&caller, |r| r.disputes_opened += 1);
    }

    // ------------------------------------------------------------------
    // Arbitration pool (decentralized dispute resolution)
    //
    // Any NEAR holder may stake NEAR to arbitrate a dispute. Votes choose the
    // seller's share of the remaining balance. Parties to a dispute may not
    // vote on their own case. Once the stake quorum is met, anyone can settle
    // — no platform-owner signature required. Voters reclaim stake after
    // finalization (pull pattern, no mass payout loops).
    // ------------------------------------------------------------------

    /// Any NEAR holder may stake NEAR to arbitrate a dispute. Votes choose the
    /// seller's share of the remaining balance. One vote per voter per case;
    /// re-voting replaces it.
    #[payable]
    pub fn vote_dispute(&mut self, escrow_id: String, seller_pct: u8) {
        require!(seller_pct <= 100, "pct must be 0..=100");
        let escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::ReviewRequired, "no open dispute for this escrow");
        let voter = env::predecessor_account_id();
        require!(voter != escrow.seller, "parties cannot arbitrate their own dispute");
        require!(Some(&voter) != escrow.buyer.as_ref(), "parties cannot arbitrate their own dispute");
        let attached = env::attached_deposit();
        require!(
            attached >= NearToken::from_yoctonear(MIN_VOTE_STAKE_YOCTO),
            "stake at least 0.01 NEAR to vote"
        );
        let stake = U128(attached.as_yoctonear());

        let mut map = arbitration_map();
        let mut case = map.get(&escrow_id).cloned().unwrap_or(ArbitrationCase {
            escrow_id: escrow_id.clone(),
            opened_ms: env::block_timestamp() / 1_000_000,
            votes: vec![],
            total_stake: U128(0),
            finalized: false,
            winning_pct: 0,
        });
        require!(!case.finalized, "arbitration already finalized");

        if let Some(existing) = case.votes.iter_mut().find(|v| v.voter == voter) {
            case.total_stake.0 -= existing.stake.0;
            existing.seller_pct = seller_pct;
            existing.stake = stake;
            existing.voted_ms = env::block_timestamp() / 1_000_000;
            existing.claimed = false;
        } else {
            require!(case.votes.len() < MAX_ARBITRATION_VOTES, "vote limit reached");
            case.votes.push(ArbitrationVote {
                voter,
                seller_pct,
                stake,
                voted_ms: env::block_timestamp() / 1_000_000,
                claimed: false,
            });
        }
        case.total_stake.0 += stake.0;
        map.insert(escrow_id, case);
    }

    /// Settlement is permissionless once the stake quorum is met: pays the
    /// stake-weighted majority's seller share and refunds the buyer the rest.
    pub fn finalize_arbitration(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::ReviewRequired, "no open dispute for this escrow");
        require!(!escrow.seller_paid, "already settled");

        let mut map = arbitration_map();
        let mut case = map.get(&escrow_id).cloned().expect("no arbitration votes for this dispute");
        require!(!case.finalized, "arbitration already finalized");
        require!(case.total_stake.0 >= MIN_ARBITRATION_STAKE_YOCTO, "arbitration quorum not reached");
        require!(!case.votes.is_empty(), "no votes yet");

        let mut tally: std::collections::BTreeMap<u8, u128> = std::collections::BTreeMap::new();
        for v in &case.votes {
            *tally.entry(v.seller_pct).or_insert(0) += v.stake.0;
        }
        let winning_pct = tally
            .into_iter()
            .max_by(|a, b| a.1.cmp(&b.1))
            .map(|(pct, _)| pct)
            .unwrap_or(0);

        case.finalized = true;
        case.winning_pct = winning_pct;
        map.insert(escrow_id.clone(), case);

        let remaining = escrow.total_amount.0 - if escrow.logistics_paid { escrow.logistics_amount.0 } else { 0 };
        let seller_part = remaining * winning_pct as u128 / 100;
        let buyer_part = remaining - seller_part;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        escrow.state = if buyer_part == 0 { EscrowState::Completed } else { EscrowState::Refunded };
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        log!("arbitration finalized for {} with {}% to seller", escrow_id, winning_pct);
        ft_transfer(token, seller, U128(seller_part)).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_arbitration_payout(escrow_id, U128(buyer_part)),
        )
    }

    #[private]
    pub fn finish_arbitration_payout(&mut self, escrow_id: String, buyer_part: U128) {
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

    /// Voters reclaim their stake after finalization. Each stake is claimable
    /// exactly once (pull pattern — no mass payout loops).
    pub fn claim_stake(&mut self, escrow_id: String) -> Promise {
        let mut map = arbitration_map();
        let mut case = map.get(&escrow_id).cloned().expect("no arbitration case for this dispute");
        require!(case.finalized, "arbitration not finalized yet");
        let voter = env::predecessor_account_id();
        let vote = case
            .votes
            .iter_mut()
            .find(|v| v.voter == voter)
            .expect("no vote from this wallet for this dispute");
        require!(!vote.claimed, "stake already claimed");
        let amount = vote.stake;
        vote.claimed = true;
        map.insert(escrow_id, case);
        Promise::new(voter).transfer(NearToken::from_yoctonear(amount.0))
    }

    /// View: full arbitration case (votes, stake, quorum, outcome).
    pub fn arbitration_status(&self, escrow_id: String) -> Option<ArbitrationCase> {
        arbitration_map().get(&escrow_id).cloned()
    }

    // ------------------------------------------------------------------
    // Governance: review board + treasury
    //
    // The platform's trust model inverts from "one hot key" to "governed":
    // disputes are decided by the board account (a Sputnik/Trezu DAO whose
    // council must approve the split as an on-chain proposal), and platform
    // fees accrue to a treasury account instead of the owner EOA.
    // ------------------------------------------------------------------

    /// Appoint the review board. Callable by the owner (bootstrap) or by the
    /// current board (handoff). Once set, ONLY the board can resolve disputes.
    pub fn set_arbiter(&mut self, account_id: AccountId) {
        let caller = env::predecessor_account_id();
        let current = self.get_arbiter();
        require!(
            caller == self.owner || Some(caller) == current,
            "only the owner or the current board can appoint the board"
        );
        gov_config_map().insert("arbiter".to_string(), account_id);
    }

    /// Review board account (None = owner bootstrap mode).
    pub fn get_arbiter(&self) -> Option<AccountId> {
        gov_config_map().get(&"arbiter".to_string()).cloned()
    }

    /// Set where platform fees accrue (e.g. a Trezu multisig treasury).
    /// Callable by the owner or the current treasury/board.
    pub fn set_treasury(&mut self, account_id: AccountId) {
        let caller = env::predecessor_account_id();
        let current = self.get_treasury();
        let board = self.get_arbiter();
        require!(
            caller == self.owner || Some(caller.clone()) == current || Some(caller) == board,
            "only the owner, board or current treasury can change the treasury"
        );
        gov_config_map().insert("treasury".to_string(), account_id);
    }

    /// Treasury account (None = owner).
    pub fn get_treasury(&self) -> Option<AccountId> {
        gov_config_map().get(&"treasury".to_string()).cloned()
    }

    /// Single-call governance snapshot for UIs.
    pub fn get_gov_info(&self) -> GovInfo {
        GovInfo {
            arbiter: self.get_arbiter(),
            treasury: self.get_treasury(),
        }
    }

    /// Where platform fees go: treasury when set, else owner.
    fn platform_fee_recipient(&self) -> AccountId {
        self.get_treasury().unwrap_or_else(|| self.owner.clone())
    }

    /// Explicit manual review path — now GOVERNED. Disputes are resolved by
    /// the review board set via `set_arbiter` (typically a Sputnik/Trezu DAO
    /// whose council approves the split as an on-chain FunctionCall proposal).
    /// Until a board is appointed the platform owner can resolve (bootstrap
    /// only); once `set_arbiter` is called the owner can no longer decide.
    pub fn resolve_review(&mut self, escrow_id: String, seller_pct: u8) -> Promise {
        let resolver = self.get_arbiter().unwrap_or_else(|| self.owner.clone());
        require!(
            env::predecessor_account_id() == resolver,
            "only the review board resolves disputes"
        );
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

/// Arbitration cases live under their own prefix, constructed locally (never
/// as a state-struct field) so no state migration is needed on upgrade.
fn arbitration_map() -> LookupMap<String, ArbitrationCase> {
    LookupMap::new(b"ar1".to_vec())
}

/// Governance configuration (review board + treasury) lives under its own
/// prefix — never a state-struct field — so upgrades need no migration.
/// While unset, the platform owner is the bootstrap resolver/fee recipient.
fn gov_config_map() -> LookupMap<String, AccountId> {
    LookupMap::new(b"gov".to_vec())
}

/// Governance snapshot for UIs (single call).
#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct GovInfo {
    /// Review board (typically a Sputnik/Trezu DAO). Once set, ONLY this
    /// account can resolve disputes — the owner fallback is replaced.
    pub arbiter: Option<AccountId>,
    /// Where platform fees accrue. Unset = owner.
    pub treasury: Option<AccountId>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::testing_env;

    fn as_account(account: AccountId) -> VMContextBuilder {
        let mut b = VMContextBuilder::new();
        b.predecessor_account_id(account)
            .account_balance(NearToken::from_near(50));
        b
    }

    fn setup() -> VeriPay {
        testing_env!(as_account(accounts(0)).build());
        VeriPay::new(accounts(0), accounts(5), "test-project".to_string(), 500)
    }

    #[test]
    fn owner_appoints_board_and_board_can_hand_off() {
        let mut c = setup();
        assert_eq!(c.get_arbiter(), None);
        c.set_arbiter(accounts(1));
        assert_eq!(c.get_arbiter(), Some(accounts(1)));
        testing_env!(as_account(accounts(1)).build());
        c.set_arbiter(accounts(2));
        assert_eq!(c.get_arbiter(), Some(accounts(2)));
    }

    #[test]
    #[should_panic(expected = "only the owner or the current board")]
    fn stranger_cannot_appoint_board() {
        let mut c = setup();
        testing_env!(as_account(accounts(5)).build());
        c.set_arbiter(accounts(1));
    }

    #[test]
    fn treasury_defaults_to_owner_then_follows_config() {
        let mut c = setup();
        assert_eq!(c.platform_fee_recipient(), accounts(0));
        c.set_treasury(accounts(3));
        assert_eq!(c.platform_fee_recipient(), accounts(3));
        // the board can also rotate the treasury
        testing_env!(as_account(accounts(0)).build());
        c.set_arbiter(accounts(1));
        testing_env!(as_account(accounts(1)).build());
        c.set_treasury(accounts(4));
        assert_eq!(c.get_treasury(), Some(accounts(4)));
    }

    #[test]
    #[should_panic(expected = "only the owner, board or current treasury")]
    fn stranger_cannot_change_treasury() {
        let mut c = setup();
        testing_env!(as_account(accounts(5)).build());
        c.set_treasury(accounts(3));
    }

    #[test]
    #[should_panic(expected = "only the review board resolves disputes")]
    fn owner_cannot_resolve_after_board_is_set() {
        let mut c = setup();
        c.set_arbiter(accounts(1));
        // Owner tries to decide a dispute — rejected BEFORE any state access.
        c.resolve_review("VP-000001".to_string(), 50);
    }

    #[test]
    #[should_panic(expected = "unknown escrow")]
    fn board_passes_resolver_auth_check() {
        let mut c = setup();
        c.set_arbiter(accounts(1));
        testing_env!(as_account(accounts(1)).build());
        // Board passes the resolver gate and fails later on the missing
        // escrow — proving the auth check itself accepts the board.
        c.resolve_review("VP-000001".to_string(), 50);
    }

    #[test]
    fn gov_info_snapshot() {
        let mut c = setup();
        assert_eq!(c.get_gov_info().arbiter, None);
        c.set_arbiter(accounts(1));
        c.set_treasury(accounts(2));
        let info = c.get_gov_info();
        assert_eq!(info.arbiter, Some(accounts(1)));
        assert_eq!(info.treasury, Some(accounts(2)));
    }
}
