//! Sweeft escrow contract (NEAR testnet).
//!
//! Financial and authorization source of truth for Sweeft agreements:
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
//! - Verification requests and logistics settlement are permissionless: the
//!   contract state machine gates them (`Shipped`, `verification_pending`,
//!   `pickup_verified`, payout flags), so a platform relayer can run the
//!   happy path to completion without user signatures. Funds can only ever
//!   move to the beneficiaries fixed at creation/funding time.
//! - The seller allocation releases on explicit buyer confirmation or, once
//!   the inspection window elapses with no problem reported, through the
//!   permissionless `release_after_inspection`.
//! - Beneficiaries are fixed at creation/funding time; no method changes them.
//! - Carrier tracking status alone never releases the seller allocation.

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::json_types::U128;
use near_sdk::serde::{Deserialize, Serialize};
use near_sdk::serde_json::json;
use near_sdk::store::{LookupMap, Vector};
use near_sdk::{env, log, near, require, AccountId, BorshStorageKey, Gas, NearToken, PanicOnDefault, Promise};


const MIN_VERIFICATION_DEPOSIT_YOCTO: u128 = 1_000_000_000_000_000_000_000; // 0.001 NEAR
const OUTLAYER_GAS_TGAS: u64 = 100;
const CALLBACK_GAS_TGAS: u64 = 20;
/// Native NEAR currency marker in `Escrow.token`. It is a valid AccountId so
/// existing serialized escrows need no migration; legacy NEP-141 escrows keep
/// using their token contract. New escrows default to this marker in the UI.
const NATIVE_NEAR_TOKEN: &str = "near";
const MIN_VOTE_STAKE_YOCTO: u128 = 10_000_000_000_000_000_000_000; // 0.01 NEAR per vote
const MIN_ARBITRATION_STAKE_YOCTO: u128 = 50_000_000_000_000_000_000_000; // 0.05 NEAR quorum
const MAX_ARBITRATION_VOTES: usize = 24;
/// Storage deposit required at creation: escrow bookkeeping is paid up front
/// instead of being funded out of pooled escrow balances (storage-drain fix).
const MIN_CREATE_DEPOSIT_YOCTO: u128 = 20_000_000_000_000_000_000_000; // 0.02 NEAR
/// Bounded strings: `create_escrow` is free to call, so unbounded input is a
/// storage-exhaustion vector.
const MAX_NAME_LEN: usize = 120;
const MAX_HASH_LEN: usize = 128;
const MAX_TERMS_LEN: usize = 256;
const MAX_TRACKING_LEN: usize = 64;
const MAX_INVITE_HASH_LEN: usize = 128;
/// Anti-spam caps for the per-account indexes (`pending_for` can otherwise be
/// used to bloat any victim's storage).
const MAX_IDS_PER_ACCOUNT: usize = 50;
const MAX_PENDING_PER_ACCOUNT: usize = 10;
const MAX_ESCROWS: u64 = 1_000_000;
/// Deals below this value still settle but do not move reputation counters
/// (wash-trading friction).
const MIN_COUNTED_AMOUNT_YOCTO: u128 = 5_000_000_000_000_000_000_000; // 0.005 NEAR
/// Inspection window floor: a verified delivery plus the permissionless
/// timeout release must leave the buyer a real window to inspect/dispute.
const MIN_INSPECTION_PERIOD_MS: u64 = 3_600_000; // 1 hour
/// Long-stop: if a shipment is registered but delivery is never verified, the
/// buyer may reclaim the unspent balance after this grace period. Prevents
/// frozen funds when dispute resolution stalls.
const LONG_STOP_MS: u64 = 30 * 24 * 60 * 60 * 1000; // 30 days
/// A dispute cannot lock the buyer's remaining funds indefinitely. If the
/// board has not resolved it within seven days, the buyer can claim the
/// remaining balance (less any logistics allocation already paid).
const DISPUTE_TIMEOUT_MS: u64 = 7 * 24 * 60 * 60 * 1000; // 7 days

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
    EscrowIds,
}

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct Sweeft {
    owner: AccountId,
    outlayer_contract: AccountId,
    outlayer_project: String,
    platform_fee_bps: u16,
    escrows: LookupMap<String, Escrow>,
    buyer_index: LookupMap<AccountId, Vec<String>>,
    seller_index: LookupMap<AccountId, Vec<String>>,
    pending_index: LookupMap<AccountId, Vec<String>>,
    escrow_ids: Vector<String>,
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
impl Sweeft {
    /// Initialization REQUIRES the review board: a fresh deployment can never
    /// expose the (deprecated) arbitration pool even briefly.
    #[init]
    pub fn new(
        owner: AccountId,
        outlayer_contract: AccountId,
        outlayer_project: String,
        platform_fee_bps: u16,
        arbiter: AccountId,
        provider_id: Option<String>,
        carrier_pubkey: Option<String>,
    ) -> Self {
        require!(platform_fee_bps <= 2_000, "fee bps too high");
        gov_config_map().insert("arbiter".to_string(), arbiter);
        if let Some(p) = provider_id {
            require!(p.len() <= 64, "provider_id too long");
            str_config_map().insert("provider_id".to_string(), p);
        }
        if let Some(k) = carrier_pubkey {
            require!(
                k.len() == 64 && k.chars().all(|c| c.is_ascii_hexdigit()),
                "carrier pubkey must be 64 hex chars"
            );
            str_config_map().insert("carrier_pubkey".to_string(), k.to_lowercase());
        }
        Self {
            owner,
            outlayer_contract,
            outlayer_project,
            platform_fee_bps,
            escrows: LookupMap::new(StorageKey::Escrows),
            buyer_index: LookupMap::new(StorageKey::BuyerIndex),
            seller_index: LookupMap::new(StorageKey::SellerIndex),
            pending_index: LookupMap::new(StorageKey::PendingIndex),
            escrow_ids: Vector::new(StorageKey::EscrowIds),
        }
    }

    /// Collision-resistant six-digit escrow code: `SWFT-XXXXXX`. The digest
    /// mixes block time, caller and the creation counter so codes look random
    /// and don't repeat for concurrent creators; a collision just re-hashes.
    fn generate_escrow_id(&self) -> String {
        let mut attempt: u64 = 0;
        loop {
            let seed = format!(
                "{}:{}:{}:{}",
                env::block_timestamp(),
                env::predecessor_account_id(),
                self.escrow_ids.len(),
                attempt
            );
            let hash = env::sha256(seed.as_bytes());
            let n = u32::from_le_bytes([hash[0], hash[1], hash[2], hash[3]]) % 1_000_000;
            let id = format!("SWFT-{:06}", n);
            if !self.escrows.contains_key(&id) {
                return id;
            }
            attempt += 1;
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
        // Creation carries its own storage deposit; escrowed balances are
        // never spent on bookkeeping.
        require!(
            env::attached_deposit().as_yoctonear() >= MIN_CREATE_DEPOSIT_YOCTO,
            "attach at least 0.02 NEAR storage deposit"
        );
        require!((self.escrow_ids.len() as u64) < MAX_ESCROWS, "escrow capacity reached");
        require!(product_name.len() <= MAX_NAME_LEN, "product_name too long");
        require!(product_desc_hash.len() <= MAX_HASH_LEN, "product_desc_hash too long");
        require!(terms_hash.len() <= MAX_TERMS_LEN, "terms_hash too long");
        if let Some(t) = &tracking_number {
            require!(t.len() <= MAX_TRACKING_LEN, "tracking_number too long");
        }
        if let Some(h) = &invite_hash {
            require!(h.len() <= MAX_INVITE_HASH_LEN, "invite_hash too long");
        }
        // Currency allowlist: native NEAR, or the single configured legacy
        // token (if one is still enabled). Sellers cannot pick arbitrary tokens.
        let allowed_token = self.get_allowed_token();
        require!(
            token.as_str() == NATIVE_NEAR_TOKEN || Some(&token) == allowed_token.as_ref(),
            "token not allowed"
        );
        if let Some(b) = &buyer {
            require!(b != &seller, "buyer must differ from seller");
        } else {
            require!(invite_hash.is_some(), "invite_hash required when buyer is not preassigned");
        }
        if let Some(p) = &logistics_provider {
            require!(p != &seller, "provider must differ from seller");
        }
        require!(dispatch_deadline_ms > env::block_timestamp() / 1_000_000, "dispatch deadline in the past");
        require!(
            inspection_period_ms >= MIN_INSPECTION_PERIOD_MS,
            "inspection period below the 1 hour floor"
        );
        let seller_ids = self.seller_index.get(&seller).cloned().unwrap_or_default();
        require!(seller_ids.len() < MAX_IDS_PER_ACCOUNT, "seller escrow index full");
        if let Some(b) = &buyer {
            let pending = self.pending_index.get(b).cloned().unwrap_or_default();
            require!(pending.len() < MAX_PENDING_PER_ACCOUNT, "counterparty pending index full");
        }

        let platform_fee = seller_amount.0 * self.platform_fee_bps as u128 / 10_000;
        let total_amount = seller_amount.0 + logistics_amount.0 + platform_fee;

        let id = self.generate_escrow_id();

        let tracking_number = tracking_number.filter(|t| !t.trim().is_empty());
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
            // A tracking id can be committed at creation, but dispatch is only
            // recorded by `register_shipment` after funding/provider assignment.
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
        self.escrow_ids.push(id.clone());
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
        let buyer_ids = self.buyer_index.get(&caller).cloned().unwrap_or_default();
        require!(buyer_ids.len() < MAX_IDS_PER_ACCOUNT, "buyer escrow index full");
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
            || token.as_str() == NATIVE_NEAR_TOKEN
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

    /// Fund a native-NEAR escrow. The buyer attaches the exact total in
    /// yoctoNEAR; no mint, token registration, or token-contract call needed.
    /// Legacy NEP-141 escrows still fund through `ft_transfer_call`.
    #[payable]
    pub fn fund_escrow(&mut self, escrow_id: String) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.token.as_str() == NATIVE_NEAR_TOKEN, "not a native NEAR escrow");
        require!(escrow.state == EscrowState::Approved, "wrong state");
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer funds");
        require!(
            env::attached_deposit().as_yoctonear() == escrow.total_amount.0,
            "attach the exact total in yoctoNEAR"
        );
        let total = escrow.total_amount.0;
        escrow.state = EscrowState::Funded;
        self.escrows.insert(escrow_id.clone(), escrow);
        log!(
            "escrow {} funded with {} yoctoNEAR by {}",
            escrow_id,
            total,
            env::predecessor_account_id()
        );
    }

    // ------------------------------------------------------------------
    // Logistics and shipment
    // ------------------------------------------------------------------

    /// Seller confirms the logistics beneficiary after funding. A provider
    /// committed at creation can only be confirmed, never replaced; otherwise
    /// the first post-funding assignment fixes it for the rest of the escrow.
    pub fn assign_logistics_provider(&mut self, escrow_id: String, provider: AccountId) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(env::predecessor_account_id() == escrow.seller, "only the seller assigns the provider");
        require!(escrow.state == EscrowState::Funded, "escrow not funded");
        require!(provider != escrow.seller, "provider must differ from seller");
        match &escrow.logistics_provider {
            Some(current) => require!(current == &provider, "logistics provider is fixed before funding"),
            None => escrow.logistics_provider = Some(provider),
        }
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
        require!(tracking_number.len() <= MAX_TRACKING_LEN, "tracking_number too long");
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
    /// Permissionless: the state machine and the deposit gate it, so the
    /// platform relayer (or any party) can trigger it — the TEE result is the
    /// only thing that can set the verification flags. Attach the OutLayer
    /// compute deposit; unused funds are refunded to this contract.
    #[payable]
    pub fn request_delivery_verification(&mut self, escrow_id: String, carrier_payload: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::Shipped, "escrow not shipped");
        require!(!escrow.verification_pending, "verification already pending");
        // Carrier authenticity is mandatory end-to-end: the contract refuses
        // to request verification until a carrier signing key is configured,
        // independent of verifier version behaviour.
        require!(self.get_carrier_pubkey().is_some(), "carrier key not configured");
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
                "provider_id": self.get_provider_id(),
                "carrier_pubkey": self.get_carrier_pubkey(),
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
            Ok(Some(mut outcome)) => {
                if outcome.escrow_id != escrow.id {
                    log!("verification escrow mismatch: {} != {}", outcome.escrow_id, escrow.id);
                    self.escrows.insert(escrow_id, escrow);
                    return None;
                }
                // The verifier must have checked the carrier report against the
                // tracking number committed on-chain; a mismatch invalidates
                // the outcome regardless of what the caller supplied.
                if let Some(expected) = escrow.tracking_number.as_ref() {
                    if &outcome.tracking_number != expected {
                        outcome.verified = false;
                        outcome.reason = format!(
                            "tracking mismatch: report '{}' vs on-chain '{}'",
                            outcome.tracking_number, expected
                        );
                    }
                }
                // Same for the carrier-registry provider id, when configured.
                if let Some(expected_provider) = self.get_provider_id() {
                    if outcome.provider_id != expected_provider {
                        outcome.verified = false;
                        outcome.reason = format!(
                            "provider mismatch: report '{}' vs configured '{}'",
                            outcome.provider_id, expected_provider
                        );
                    }
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
                        // Start the inspection-window clock (the permissionless
                        // timeout release reads this timestamp).
                        self.note_delivery_time(&escrow_id);
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
    /// Permissionless: the amount and the beneficiary are fixed by the escrow,
    /// so anyone (seller, provider, or the platform relayer) can trigger the
    /// payout. A failed FT transfer rolls the flag back (see
    /// `on_payout_result`) so the payout can be retried.
    pub fn settle_logistics(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.pickup_verified, "pickup not verified yet");
        require!(!escrow.logistics_paid, "logistics already paid");
        // Disputed or settled escrows must not pay logistics again: resolution
        // paths fold an unpaid logistics allocation into the split and mark
        // this flag, so a later call here would double-pay from pooled funds.
        require!(
            escrow.state == EscrowState::Shipped || escrow.state == EscrowState::AwaitingBuyerConfirmation,
            "escrow not in a settleable state"
        );
        let provider = escrow.logistics_provider.clone().expect("provider not assigned");
        escrow.logistics_paid = true;
        let amount = escrow.logistics_amount;
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        payout(token, provider, amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "logistics".to_string()),
        )
    }

    /// Buyer confirms receipt after TEE-verified delivery.
    pub fn confirm_receipt(&mut self, escrow_id: String) -> Promise {
        require!(
            Some(&env::predecessor_account_id()) == self.internal_get(&escrow_id).buyer.as_ref(),
            "only the buyer confirms receipt"
        );
        self.start_completion(&escrow_id)
    }

    /// Permissionless timeout release: once delivery is TEE-verified and the
    /// inspection window has elapsed with no problem reported, anyone (e.g.
    /// the platform relayer) can release the allocations.
    pub fn release_after_inspection(&mut self, escrow_id: String) -> Promise {
        let escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::AwaitingBuyerConfirmation, "wrong state");
        let times = delivery_times_map();
        let delivered_ms = *times.get(&escrow_id).expect("delivery time not recorded");
        require!(
            env::block_timestamp() / 1_000_000 >= delivered_ms.saturating_add(escrow.inspection_period_ms),
            "inspection window not elapsed"
        );
        self.start_completion(&escrow_id)
    }

    /// Shared completion sequence. Pays in order: logistics (if still due),
    /// seller, then platform fee. Each leg is flag-guarded and rolls back
    /// exactly its own flag, so no allocation can strand (logistics included)
    /// and none can pay twice. When logistics is still due, a recorded
    /// provider is required — completion never leaves that share behind.
    fn start_completion(&mut self, escrow_id: &str) -> Promise {
        let mut escrow = self.internal_get(escrow_id);
        // Idempotency first: a retry after settlement must report clearly.
        require!(!escrow.seller_paid, "already settled");
        require!(escrow.state == EscrowState::AwaitingBuyerConfirmation, "wrong state");
        require!(escrow.delivered_verified, "delivery not verified");
        require!(!escrow.problem_reported, "problem reported; resolve review first");

        let logistics_due_now = !escrow.logistics_paid;
        if logistics_due_now {
            require!(escrow.pickup_verified, "pickup not verified");
            require!(escrow.logistics_provider.is_some(), "assign a logistics provider before completion");
            escrow.logistics_paid = true;
        }
        escrow.state = EscrowState::Completed;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        let seller_amount = escrow.seller_amount;
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        let provider = escrow.logistics_provider.clone();
        let logistics_amount = escrow.logistics_amount;
        self.escrows.insert(escrow_id.to_string(), escrow);

        if logistics_due_now {
            payout(token, provider.expect("provider checked"), logistics_amount).then(
                Self::ext(env::current_account_id())
                    .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                    .finish_completion_logistics(escrow_id.to_string()),
            )
        } else {
            payout(token, seller, seller_amount).then(
                Self::ext(env::current_account_id())
                    .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                    .finish_platform(escrow_id.to_string()),
            )
        }
    }

    /// After the logistics leg of a completion: continue with seller +
    /// platform, or roll the completion back so it can be retried. A failed
    /// logistics transfer rolls back all completion flags (nothing was paid).
    #[private]
    pub fn finish_completion_logistics(&mut self, escrow_id: String) {
        if !payout_ok(0) {
            let mut escrow = self.internal_get(&escrow_id);
            escrow.logistics_paid = false;
            escrow.seller_paid = false;
            escrow.platform_paid = false;
            escrow.state = EscrowState::AwaitingBuyerConfirmation;
            self.escrows.insert(escrow_id, escrow);
            log!("completion logistics payout failed — rolled back");
            return;
        }
        let escrow = self.internal_get(&escrow_id);
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        let seller_amount = escrow.seller_amount;
        let _ = payout(token, seller, seller_amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_platform(escrow_id),
        );
    }

    /// After the seller transfer settles: pay the platform fee, or roll the
    /// completion flags back so the buyer can retry.
    #[private]
    pub fn finish_platform(&mut self, escrow_id: String) {
        self.handle_seller_payout(escrow_id, payout_ok(0));
    }

    /// Reputation is a receipt of settlement, not merely an attempt. Count the
    /// completion only after the seller transfer succeeds; failed seller or
    /// logistics legs roll back without leaving a completion on either profile.
    fn handle_seller_payout(&mut self, escrow_id: String, succeeded: bool) {
        if !succeeded {
            let mut escrow = self.internal_get(&escrow_id);
            escrow.seller_paid = false;
            escrow.platform_paid = false;
            escrow.state = EscrowState::AwaitingBuyerConfirmation;
            self.escrows.insert(escrow_id, escrow);
            return;
        }
        let escrow = self.internal_get(&escrow_id);
        self.record_completion(&escrow);
        let promise = payout(escrow.token.clone(), self.platform_fee_recipient(), escrow.platform_fee);
        let _ = promise.then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "platform".to_string()),
        );
    }

    fn record_completion(&mut self, escrow: &Escrow) {
        if counted(&escrow.id, COUNTED_COMPLETION) || escrow.seller_amount.0 < MIN_COUNTED_AMOUNT_YOCTO {
            return;
        }
        mark_counted(&escrow.id, COUNTED_COMPLETION);
        let seller_amount = escrow.seller_amount;
        bump_reputation(&escrow.seller, |r| {
            r.completed_as_seller += 1;
            r.volume.0 += seller_amount.0;
        });
        let buyer = escrow.buyer.clone().expect("buyer set");
        bump_reputation(&buyer, |r| {
            r.completed_as_buyer += 1;
            r.volume.0 += seller_amount.0;
        });
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
        payout(token, self.platform_fee_recipient(), platform_fee).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "platform".to_string()),
        )
    }

    /// Rolls a payout flag back when its transfer failed (idempotency guard).
    /// Each leg is independent: a failed buyer leg never un-marks the seller
    /// leg, so a resolved dispute can retry only what is still owed.
    #[private]
    pub fn on_payout_result(&mut self, escrow_id: String, party: String) {
        self.handle_payout_result(escrow_id, party, payout_ok(0));
    }

    fn handle_payout_result(&mut self, escrow_id: String, party: String, succeeded: bool) {
        if succeeded {
            match party.as_str() {
                "due_buyer" => {
                    review_due_map().insert(escrow_id, U128(0));
                }
                "refund" => {
                    refund_return_state_map().remove(&escrow_id);
                    // A fallback refund resolves any legacy dispute case too;
                    // otherwise voter stakes would remain locked forever.
                    close_arbitration_case(&escrow_id, 0);
                    if let Some(escrow) = self.escrows.get(&escrow_id).cloned() {
                        if !counted(&escrow_id, COUNTED_REFUND) {
                            mark_counted(&escrow_id, COUNTED_REFUND);
                            let buyer = escrow.buyer.clone().expect("buyer set");
                            bump_reputation(&buyer, |r| r.refunds_claimed += 1);
                        }
                    }
                }
                _ => {}
            }
            return;
        }
        let Some(mut escrow) = self.escrows.get(&escrow_id).cloned() else { return };
        match party.as_str() {
            "logistics" => escrow.logistics_paid = false,
            "platform" => escrow.platform_paid = false,
            "refund" => {
                // Restore Shipped/ReviewRequired too: resetting every failed
                // refund to Funded used to strand long-stop refunds.
                escrow.state = refund_return_state_map()
                    .remove(&escrow_id)
                    .unwrap_or(EscrowState::Funded);
            }
            "due_buyer" => {
                // Seller/platform stay marked paid; only the buyer remainder
                // is outstanding and retryable via `retry_review_buyer_payout`.
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
        require!(reason_hash.len() <= MAX_HASH_LEN, "reason_hash too long");
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer reports a problem");
        require!(
            escrow.state == EscrowState::Shipped || escrow.state == EscrowState::AwaitingBuyerConfirmation,
            "wrong state"
        );
        escrow.problem_reported = true;
        escrow.terms_hash = format!("{}|problem:{}", escrow.terms_hash, reason_hash);
        escrow.state = EscrowState::ReviewRequired;
        self.note_dispute_time(&escrow_id);
        self.escrows.insert(escrow_id, escrow);
        bump_reputation(&env::predecessor_account_id(), |r| r.disputes_opened += 1);
    }

    /// Either party can open a dispute. The buyer may escalate from
    /// shipped/awaiting-confirmation states (same as report_problem); the
    /// seller may additionally escalate from funded/shipment-pending states
    /// (e.g. an unresponsive buyer). The seven-day dispute refund prevents
    /// either party or an inactive board from locking the buyer's funds forever.
    pub fn open_dispute(&mut self, escrow_id: String, reason_hash: String) {
        let mut escrow = self.internal_get(&escrow_id);
        require!(reason_hash.len() <= MAX_HASH_LEN, "reason_hash too long");
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
        self.note_dispute_time(&escrow_id);
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

    /// DEPRECATED pool path. Disabled whenever a review board is set: with an
    /// open pool, a seller could sockpuppet-vote and settle without shipping,
    /// recovering the stake afterwards. The review board is the only
    /// resolution path once `set_arbiter` has been called.
    #[payable]
    pub fn vote_dispute(&mut self, escrow_id: String, seller_pct: u8) {
        require!(self.get_arbiter().is_none(), "arbitration pool is disabled while a review board is set");
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
        // One vote per wallet. Re-voting used to overwrite the stake and lose
        // the previous deposit; now the first vote is final until the case is
        // resolved and claimed.
        require!(
            !case.votes.iter().any(|v| v.voter == voter),
            "already voted — stake is locked until the case resolves"
        );
        require!(case.votes.len() < MAX_ARBITRATION_VOTES, "vote limit reached");
        case.votes.push(ArbitrationVote {
            voter,
            seller_pct,
            stake,
            voted_ms: env::block_timestamp() / 1_000_000,
            claimed: false,
        });
        case.total_stake.0 += stake.0;
        map.insert(escrow_id, case);
    }

    /// Settlement is permissionless once the stake quorum is met. Disabled
    /// while a review board is set (see `vote_dispute`). Ties choose the
    /// buyer-favourable (lower) seller share.
    pub fn finalize_arbitration(&mut self, escrow_id: String) -> Promise {
        require!(self.get_arbiter().is_none(), "arbitration pool is disabled while a review board is set");
        let mut escrow = self.internal_get(&escrow_id);
        require!(escrow.state == EscrowState::ReviewRequired, "no open dispute for this escrow");
        require!(!escrow.seller_paid, "already settled");
        require!(review_due(&escrow_id) == 0, "buyer payout pending retry");

        let mut map = arbitration_map();
        let mut case = map.get(&escrow_id).cloned().expect("no arbitration votes for this dispute");
        require!(!case.finalized, "arbitration already finalized");
        require!(case.total_stake.0 >= MIN_ARBITRATION_STAKE_YOCTO, "arbitration quorum not reached");
        require!(!case.votes.is_empty(), "no votes yet");

        let mut tally: std::collections::BTreeMap<u8, u128> = std::collections::BTreeMap::new();
        for v in &case.votes {
            *tally.entry(v.seller_pct).or_insert(0) += v.stake.0;
        }
        // Buyer-favourable tie-break: on equal stake the lower seller share wins.
        let mut winning_pct = 0u8;
        let mut best_stake = 0u128;
        for (pct, stake) in tally {
            if stake > best_stake || (stake == best_stake && pct < winning_pct) {
                best_stake = stake;
                winning_pct = pct;
            }
        }

        case.finalized = true;
        case.winning_pct = winning_pct;
        map.insert(escrow_id.clone(), case);

        // Same ordering rule as `resolve_review`: compute the pot from the
        // pre-flip logistics state so an unpaid allocation is folded into the
        // split rather than stranded.
        let logistics_deduction = if escrow.logistics_paid { escrow.logistics_amount.0 } else { 0 };
        let remaining = escrow.total_amount.0 - logistics_deduction;
        resolution_logistics_map().insert(escrow_id.clone(), escrow.logistics_paid);
        escrow.logistics_paid = true;
        let seller_part = remaining * winning_pct as u128 / 100;
        let buyer_part = remaining - seller_part;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        escrow.state = if buyer_part == 0 { EscrowState::Completed } else { EscrowState::Refunded };
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        review_due_map().insert(escrow_id.clone(), U128(buyer_part));
        log!("arbitration finalized for {} with {}% to seller", escrow_id, winning_pct);
        payout(token, seller, U128(seller_part)).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_arbitration_payout(escrow_id),
        )
    }

    #[private]
    pub fn finish_arbitration_payout(&mut self, escrow_id: String) {
        self.handle_resolution_payout(escrow_id, payout_ok(0), "arbitration");
    }

    fn handle_resolution_payout(&mut self, escrow_id: String, succeeded: bool, label: &str) {
        if !succeeded {
            review_due_map().insert(escrow_id.clone(), U128(0));
            let mut escrow = self.internal_get(&escrow_id);
            escrow.seller_paid = false;
            escrow.platform_paid = false;
            escrow.logistics_paid = resolution_logistics_map()
                .remove(&escrow_id)
                .unwrap_or(true);
            escrow.state = EscrowState::ReviewRequired;
            self.escrows.insert(escrow_id.clone(), escrow);
            // The disabled legacy pool remains internally consistent if ever
            // used without a board: a failed seller leg can be finalized again.
            let mut map = arbitration_map();
            if let Some(mut case) = map.get(&escrow_id).cloned() {
                case.finalized = false;
                map.insert(escrow_id.clone(), case);
            }
            log!("{} seller payout failed — returned to ReviewRequired", label);
            return;
        }
        resolution_logistics_map().remove(&escrow_id);
        self.pay_review_due(&escrow_id);
    }

    /// Voters reclaim their stake after finalization. Each stake is claimable
    /// exactly once (pull pattern — no mass payout loops).
    pub fn claim_stake(&mut self, escrow_id: String) -> Promise {
        require!(
            resolution_logistics_map().get(&escrow_id).is_none(),
            "resolution payout pending"
        );
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

    /// Appoint the review board. The owner can only BOOTSTRAP it (while unset);
    /// once a board exists, only the board itself can rotate it — the owner
    /// can no longer take resolution power back.
    pub fn set_arbiter(&mut self, account_id: AccountId) {
        let caller = env::predecessor_account_id();
        match self.get_arbiter() {
            Some(current) => require!(
                caller == current,
                "board already set — only the current board can change it"
            ),
            None => require!(caller == self.owner, "only the owner can bootstrap the board"),
        }
        gov_config_map().insert("arbiter".to_string(), account_id);
    }

    /// Allowed legacy token (None = native NEAR only). Settable by the owner
    /// or the board; escrow creators can never pick an arbitrary token.
    pub fn set_allowed_token(&mut self, token: Option<AccountId>) {
        let caller = env::predecessor_account_id();
        require!(
            Some(caller) == self.get_arbiter(),
            "only the board can set the allowed token"
        );
        let value = token.unwrap_or_else(|| NATIVE_NEAR_TOKEN.parse().unwrap());
        gov_config_map().insert("allowed_token".to_string(), value);
    }

    /// The configured legacy token, if any (only meaningful for old escrows).
    pub fn get_allowed_token(&self) -> Option<AccountId> {
        let stored = gov_config_map().get(&"allowed_token".to_string()).cloned();
        match stored {
            Some(t) if t.as_str() != NATIVE_NEAR_TOKEN => Some(t),
            _ => None,
        }
    }

    /// Carrier-registry provider id required in verification reports (e.g.
    /// "LOG-DEMO-001"). Board-only: the verification trust root must never be
    /// a single operator key.
    pub fn set_provider_id(&mut self, provider_id: Option<String>) {
        let caller = env::predecessor_account_id();
        require!(
            Some(caller) == self.get_arbiter(),
            "only the board can set the provider id"
        );
        if let Some(p) = &provider_id {
            require!(p.len() <= 64, "provider_id too long");
        }
        let mut map = str_config_map();
        match provider_id {
            Some(p) => map.insert("provider_id".to_string(), p),
            None => map.remove(&"provider_id".to_string()),
        };
    }

    /// Configured carrier provider id, if any.
    pub fn get_provider_id(&self) -> Option<String> {
        str_config_map().get(&"provider_id".to_string()).cloned()
    }

    /// Carrier signing key (hex ed25519 pubkey). When configured, the TEE
    /// verifier requires a valid carrier signature over the canonical payload
    /// — caller-supplied "public" ids alone are no longer sufficient.
    pub fn set_carrier_pubkey(&mut self, pubkey: Option<String>) {
        let caller = env::predecessor_account_id();
        require!(
            Some(caller) == self.get_arbiter(),
            "only the board can set the carrier key"
        );
        if let Some(p) = &pubkey {
            require!(p.len() == 64 && p.chars().all(|c| c.is_ascii_hexdigit()), "pubkey must be 64 hex chars");
        }
        let mut map = str_config_map();
        match pubkey {
            Some(p) => map.insert("carrier_pubkey".to_string(), p.to_lowercase()),
            None => map.remove(&"carrier_pubkey".to_string()),
        };
    }

    /// Configured carrier signing key, if any.
    pub fn get_carrier_pubkey(&self) -> Option<String> {
        str_config_map().get(&"carrier_pubkey".to_string()).cloned()
    }

    /// Review board account (None = owner bootstrap mode).
    pub fn get_arbiter(&self) -> Option<AccountId> {
        gov_config_map().get(&"arbiter".to_string()).cloned()
    }

    /// Set where platform fees accrue (e.g. a Trezu multisig treasury). The
    /// owner can only bootstrap it while unset; afterwards only the current
    /// treasury or the board can rotate it.
    pub fn set_treasury(&mut self, account_id: AccountId) {
        let caller = env::predecessor_account_id();
        match self.get_treasury() {
            Some(current) => require!(
                caller == current || Some(caller) == self.get_arbiter(),
                "treasury already set — only the treasury or board can change it"
            ),
            None => require!(caller == self.owner, "only the owner can bootstrap the treasury"),
        }
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
        require!(review_due(&escrow_id) == 0, "buyer payout pending retry");
        // Compute the split pot BEFORE flipping the logistics flag: an unpaid
        // logistics allocation is folded into the split (it goes to the
        // parties), so it must NOT be subtracted from `remaining`. Subtracting
        // it after marking it paid would strand that share in the contract.
        let logistics_deduction = if escrow.logistics_paid { escrow.logistics_amount.0 } else { 0 };
        let remaining = escrow.total_amount.0 - logistics_deduction;
        // Record that no separate logistics payout will happen (folded above);
        // `settle_logistics` is also state-gated, so this can never double-pay.
        resolution_logistics_map().insert(escrow_id.clone(), escrow.logistics_paid);
        escrow.logistics_paid = true;
        // Unlock any open arbitration stakes (the case is closed by the board).
        close_arbitration_case(&escrow_id, seller_pct);
        let seller_part = remaining * seller_pct as u128 / 100;
        let buyer_part = remaining - seller_part;
        escrow.seller_paid = true;
        escrow.platform_paid = true;
        escrow.state = if buyer_part == 0 { EscrowState::Completed } else { EscrowState::Refunded };
        let seller = escrow.seller.clone();
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        review_due_map().insert(escrow_id.clone(), U128(buyer_part));
        payout(token, seller, U128(seller_part)).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .finish_review_payout(escrow_id),
        )
    }

    /// Buyer leg of a review/arbitration split. The due amount is read from a
    /// side map (not the callback argument), so a failure can always be
    /// retried without re-splitting or double-paying the seller.
    #[private]
    pub fn finish_review_payout(&mut self, escrow_id: String) {
        self.handle_resolution_payout(escrow_id, payout_ok(0), "review");
    }

    /// Permissionless retry for the buyer leg of a resolved dispute. Funds can
    /// only ever go to the recorded buyer; the due amount is cleared on
    /// success.
    pub fn retry_review_buyer_payout(&mut self, escrow_id: String) {
        let escrow = self.internal_get(&escrow_id);
        require!(escrow.seller_paid, "seller leg not settled");
        require!(review_due(&escrow_id) > 0, "no buyer payout due");
        self.pay_review_due(&escrow_id);
    }

    /// Pays the stored buyer due (if any) and keeps the due entry until the
    /// callback confirms success.
    fn pay_review_due(&mut self, escrow_id: &str) {
        let due = review_due(escrow_id);
        if due == 0 {
            review_due_map().insert(escrow_id.to_string(), U128(0));
            return;
        }
        let escrow = self.internal_get(escrow_id);
        let buyer = escrow.buyer.clone().expect("no buyer");
        let promise = payout(escrow.token.clone(), buyer, U128(due));
        let _ = promise.then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id.to_string(), "due_buyer".to_string()),
        );
    }

    /// Buyer refund paths:
    ///  1. Seller never dispatched: after the dispatch deadline with no
    ///     shipment registered, including when the seller opened a dispute.
    ///  2. Long-stop: delivery was never TEE-verified within `LONG_STOP_MS`
    ///     after the dispatch deadline, including while a dispute is open.
    ///  3. Dispute timeout: seven days after a problem was first reported,
    ///     the buyer receives the remaining pot if the board has not resolved.
    /// A resolved seller leg or outstanding buyer due can never be refunded a
    /// second time. Logistics is deducted only if that transfer already paid.
    pub fn refund_buyer(&mut self, escrow_id: String) -> Promise {
        let mut escrow = self.internal_get(&escrow_id);
        require!(Some(&env::predecessor_account_id()) == escrow.buyer.as_ref(), "only the buyer can claim this refund");
        require!(!escrow.seller_paid, "seller payout already settled");
        require!(review_due(&escrow_id) == 0, "resolved buyer payout pending retry");
        let now = env::block_timestamp() / 1_000_000;
        let in_review = escrow.state == EscrowState::ReviewRequired;
        let no_dispatch = (escrow.state == EscrowState::Funded
            || escrow.state == EscrowState::ShipmentPending
            || in_review)
            && !escrow.shipment_registered
            && now >= escrow.dispatch_deadline_ms;
        let long_stop = (escrow.state == EscrowState::Shipped || in_review)
            && !escrow.delivered_verified
            && now >= escrow.dispatch_deadline_ms.saturating_add(LONG_STOP_MS);
        let dispute_timeout = in_review
            && dispute_times_map()
                .get(&escrow_id)
                .is_some_and(|opened_ms| now >= opened_ms.saturating_add(DISPUTE_TIMEOUT_MS));
        require!(no_dispatch || long_stop || dispute_timeout, "refund not available yet");
        let logistics_deduction = if escrow.logistics_paid { escrow.logistics_amount.0 } else { 0 };
        let amount = U128(escrow.total_amount.0 - logistics_deduction);
        refund_return_state_map().insert(escrow_id.clone(), escrow.state.clone());
        escrow.state = EscrowState::Refunded;
        let buyer = escrow.buyer.clone().unwrap();
        let token = escrow.token.clone();
        self.escrows.insert(escrow_id.clone(), escrow);
        payout(token, buyer, amount).then(
            Self::ext(env::current_account_id())
                .with_static_gas(Gas::from_tgas(CALLBACK_GAS_TGAS))
                .on_payout_result(escrow_id, "refund".to_string()),
        )
    }

    // NOTE: there is deliberately NO `withdraw` and NO `withdraw_ft`. Escrowed
    // NEAR/tokens share the contract balance, so any owner sweep would be able
    // to drain funds held for active escrows — it would contradict the
    // "no platform key" guarantee. Value leaves only through settlement,
    // arbitration (when enabled), or board resolution paths.

    pub fn update_platform_fee_bps(&mut self, bps: u16) {
        let caller = env::predecessor_account_id();
        require!(
            caller == self.owner || Some(caller) == self.get_arbiter(),
            "only the owner or board can update the fee"
        );
        require!(bps <= 2_000, "fee bps too high");
        self.platform_fee_bps = bps;
    }

    // ------------------------------------------------------------------
    // Views
    // ------------------------------------------------------------------

    pub fn get_escrow(&self, escrow_id: String) -> Option<Escrow> {
        self.escrows.get(&escrow_id).cloned()
    }

    /// Unix ms when delivery was first TEE-verified — the inspection-window
    /// clock start. `None` for escrows that never reached verified delivery.
    pub fn delivery_time_ms(&self, escrow_id: String) -> Option<u64> {
        let times = delivery_times_map();
        times.get(&escrow_id).copied()
    }

    /// Unix ms when the current dispute was opened. The buyer's fallback
    /// refund becomes available at this time plus `DISPUTE_TIMEOUT_MS`.
    pub fn dispute_time_ms(&self, escrow_id: String) -> Option<u64> {
        dispute_times_map().get(&escrow_id).copied()
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
        // `start` is 1-based (first escrow = 1). Ids are random, so walk the
        // creation-order index instead of reconstructing them from a counter.
        let mut i = start.saturating_sub(1).min(self.escrow_ids.len() as u64) as u32;
        while (out.len() as u64) < limit && i < self.escrow_ids.len() {
            if let Some(id) = self.escrow_ids.get(i) {
                if let Some(e) = self.escrows.get(id) {
                    out.push(e.clone());
                }
            }
            i += 1;
        }
        out
    }

    pub fn escrow_count(&self) -> u64 {
        self.escrow_ids.len() as u64
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

    /// Starts the inspection-window clock exactly once — the first time
    /// delivery is TEE-verified. Kept in a side map (`delivery_times_map`).
    fn note_delivery_time(&mut self, escrow_id: &str) {
        let mut times = delivery_times_map();
        let key = escrow_id.to_string();
        if times.get(&key).is_none() {
            times.insert(key, env::block_timestamp() / 1_000_000);
        }
    }

    fn note_dispute_time(&mut self, escrow_id: &str) {
        let mut times = dispute_times_map();
        let key = escrow_id.to_string();
        if times.get(&key).is_none() {
            times.insert(key, env::block_timestamp() / 1_000_000);
        }
    }
}

fn pop_index(map: &mut LookupMap<AccountId, Vec<String>>, key: &AccountId, id: &str) {
    let mut v = map.get(key).cloned().unwrap_or_default();
    v.retain(|x| x != id);
    map.insert(key.clone(), v);
}

/// Settlement payout. Native escrows transfer yoctoNEAR directly; legacy
/// NEP-141 escrows call `ft_transfer` (1 yoctoNEAR required by the standard).
/// Both return a Promise whose result is checked by the same rollback
/// callbacks, so state transitions and failure handling are identical.
fn payout(token: AccountId, receiver_id: AccountId, amount: U128) -> Promise {
    if token.as_str() == NATIVE_NEAR_TOKEN {
        return Promise::new(receiver_id).transfer(NearToken::from_yoctonear(amount.0));
    }
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
// positional, so adding fields to `Sweeft` would brick existing state on
// upgrade. Collections built locally from a constant prefix share the same
// storage without touching the serialized struct at all.
// ---------------------------------------------------------------------------

fn reputation_map() -> LookupMap<AccountId, Reputation> {
    LookupMap::new(b"r1".to_vec())
}

/// Inspection-window clock: escrow id -> ms when delivery was first verified.
/// Kept OUT of the contract state struct for the same upgrade-safety reason as
/// reputation (Borsh is positional): a local collection under its own storage
/// prefix needs no state migration on upgrade.
fn delivery_times_map() -> LookupMap<String, u64> {
    LookupMap::new(b"dt1".to_vec())
}

/// Dispute clock: escrow id -> first transition to ReviewRequired.
fn dispute_times_map() -> LookupMap<String, u64> {
    LookupMap::new(b"dp1".to_vec())
}

/// State to restore when a refund transfer fails. Keeping this separately
/// avoids treating every refund as a no-dispatch refund.
fn refund_return_state_map() -> LookupMap<String, EscrowState> {
    LookupMap::new(b"rr1".to_vec())
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

/// Outstanding buyer payout for a resolved dispute (review board or pool).
/// Kept in a side map so a failed buyer transfer is retryable without
/// re-splitting — and can never double-pay the seller leg.
fn review_due_map() -> LookupMap<String, U128> {
    LookupMap::new(b"rd1".to_vec())
}

/// Whether logistics had actually been paid before a resolution attempted to
/// fold it into the split. Restored when the seller leg fails.
fn resolution_logistics_map() -> LookupMap<String, bool> {
    LookupMap::new(b"rl1".to_vec())
}

fn review_due(escrow_id: &str) -> u128 {
    review_due_map().get(&escrow_id.to_string()).copied().unwrap_or(U128(0)).0
}

/// Marks any open arbitration case as finalized so voter stakes become
/// claimable after the board resolves the dispute directly.
fn close_arbitration_case(escrow_id: &str, winning_pct: u8) {
    let mut map = arbitration_map();
    if let Some(mut case) = map.get(&escrow_id.to_string()).cloned() {
        case.finalized = true;
        case.winning_pct = winning_pct;
        map.insert(escrow_id.to_string(), case);
    }
}

/// Reputation idempotency + anti-wash filters. Bits: 1 = completion counted,
/// 2 = refund counted. Side map again, for upgrade safety.
fn counted_map() -> LookupMap<String, u8> {
    LookupMap::new(b"rc1".to_vec())
}

const COUNTED_COMPLETION: u8 = 1;
const COUNTED_REFUND: u8 = 2;

fn counted(escrow_id: &str, bit: u8) -> bool {
    counted_map()
        .get(&escrow_id.to_string())
        .copied()
        .unwrap_or(0)
        & bit
        != 0
}

fn mark_counted(escrow_id: &str, bit: u8) {
    let mut map = counted_map();
    let key = escrow_id.to_string();
    let value = map.get(&key).copied().unwrap_or(0) | bit;
    map.insert(key, value);
}

/// Governance configuration (review board + treasury) lives under its own
/// prefix — never a state-struct field — so upgrades need no migration.
/// While unset, the platform owner is the bootstrap resolver/fee recipient.
fn gov_config_map() -> LookupMap<String, AccountId> {
    LookupMap::new(b"gov".to_vec())
}

/// String-valued config (carrier provider id), same upgrade-safe pattern.
fn str_config_map() -> LookupMap<String, String> {
    LookupMap::new(b"cfg1".to_vec())
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
        // 1_000 NEAR so promise transfers (seller/logistics/platform payouts)
        // pass the mock's balance check.
        b.predecessor_account_id(account)
            .account_balance(NearToken::from_near(1_000));
        b
    }

    fn setup() -> Sweeft {
        testing_env!(as_account(accounts(0)).build());
        Sweeft::new(
            accounts(0),
            accounts(5),
            "test-project".to_string(),
            500,
            accounts(1), // review board is REQUIRED at init — pool is never exposed
            Some("LOG-DEMO-001".to_string()),
            Some("ab".repeat(32)), // carrier key required before verification
        )
    }

    #[test]
    fn init_requires_and_sets_the_board_and_carrier_config() {
        let c = setup();
        // A fresh deployment can never expose the pool: the board is set at init.
        assert_eq!(c.get_arbiter(), Some(accounts(1)));
        assert_eq!(c.get_provider_id(), Some("LOG-DEMO-001".to_string()));
    }

    #[test]
    fn board_can_hand_off() {
        let mut c = setup();
        testing_env!(as_account(accounts(1)).build());
        c.set_arbiter(accounts(2));
        assert_eq!(c.get_arbiter(), Some(accounts(2)));
    }

    #[test]
    #[should_panic(expected = "board already set")]
    fn stranger_cannot_appoint_board() {
        let mut c = setup();
        testing_env!(as_account(accounts(5)).build());
        c.set_arbiter(accounts(5));
    }

    #[test]
    #[should_panic(expected = "board already set")]
    fn owner_cannot_retake_the_board() {
        let mut c = setup();
        // Owner tries to claw resolution power back — refused.
        testing_env!(as_account(accounts(0)).build());
        c.set_arbiter(accounts(0));
    }

    #[test]
    #[should_panic(expected = "arbitration pool is disabled while a review board is set")]
    fn pool_is_disabled_once_board_is_set() {
        let mut c = setup();
        testing_env!(as_account(accounts(5)).attached_deposit(NearToken::from_millinear(10)).build());
        c.vote_dispute("SWFT-000001".to_string(), 100);
    }

    #[test]
    fn treasury_defaults_to_owner_then_follows_config() {
        let mut c = setup();
        assert_eq!(c.platform_fee_recipient(), accounts(0));
        c.set_treasury(accounts(3));
        assert_eq!(c.platform_fee_recipient(), accounts(3));
        // the board can rotate the treasury afterwards
        testing_env!(as_account(accounts(1)).build());
        c.set_treasury(accounts(4));
        assert_eq!(c.get_treasury(), Some(accounts(4)));
    }

    #[test]
    #[should_panic(expected = "only the owner can bootstrap the treasury")]
    fn stranger_cannot_change_treasury() {
        let mut c = setup();
        testing_env!(as_account(accounts(5)).build());
        c.set_treasury(accounts(3));
    }

    #[test]
    #[should_panic(expected = "only the review board resolves disputes")]
    fn owner_cannot_resolve_after_board_is_set() {
        let mut c = setup();
        // Owner tries to decide a dispute — rejected BEFORE any state access.
        c.resolve_review("SWFT-000001".to_string(), 50);
    }

    #[test]
    #[should_panic(expected = "unknown escrow")]
    fn board_passes_resolver_auth_check() {
        let mut c = setup();
        testing_env!(as_account(accounts(1)).build());
        // Board passes the resolver gate and fails later on the missing
        // escrow — proving the auth check itself accepts the board.
        c.resolve_review("SWFT-000001".to_string(), 50);
    }

    #[test]
    fn gov_info_snapshot() {
        let mut c = setup();
        c.set_treasury(accounts(2));
        let info = c.get_gov_info();
        assert_eq!(info.arbiter, Some(accounts(1)));
        assert_eq!(info.treasury, Some(accounts(2)));
    }

    #[test]
    fn escrow_ids_are_random_swft_codes_and_page_in_order() {
        let mut c = setup();
        let first = create_native(&mut c);
        let second = create_native(&mut c);
        for id in [&first, &second] {
            assert!(id.starts_with("SWFT-"), "unexpected id: {id}");
            assert_eq!(id.len(), 11);
            assert!(id[5..].chars().all(|ch| ch.is_ascii_digit()));
        }
        assert_ne!(first, second);
        let page = c.escrows_range(1, 10);
        assert_eq!(page.len(), 2);
        assert_eq!(page[0].id, first);
        assert_eq!(page[1].id, second);
        assert_eq!(c.escrow_count(), 2);
    }

    fn create_native(c: &mut Sweeft) -> String {
        create_native_with_provider(c, None)
    }

    fn create_native_with_provider(c: &mut Sweeft, provider: Option<AccountId>) -> String {
        create_native_with_provider_and_tracking(c, provider, None)
    }

    fn create_native_with_provider_and_tracking(
        c: &mut Sweeft,
        provider: Option<AccountId>,
        tracking_number: Option<String>,
    ) -> String {
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.create_escrow(
            "Native escrow".to_string(),
            "hash".to_string(),
            U128(100_000_000_000_000_000_000_000_000),
            U128(10_000_000_000_000_000_000_000_000),
            Some(accounts(2)),
            None,
            "near".parse().unwrap(),
            1_000,
            MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(),
            provider,
            tracking_number,
        )
    }

    #[test]
    fn native_fund_moves_to_funded() {
        let mut c = setup();
        let total = 115_000_000_000_000_000_000_000_000u128;
        let id = create_native(&mut c);
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_yoctonear(total)).build());
        c.fund_escrow(id.clone());
        assert_eq!(c.get_escrow(id).unwrap().state, EscrowState::Funded);
    }

    #[test]
    fn precommitted_provider_can_be_confirmed_after_funding() {
        let mut c = setup();
        let id = create_native_with_provider(&mut c, Some(accounts(4)));
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_yoctonear(115_000_000_000_000_000_000_000_000))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(1)).build());
        c.assign_logistics_provider(id.clone(), accounts(4));
        let escrow = c.get_escrow(id).unwrap();
        assert_eq!(escrow.state, EscrowState::ShipmentPending);
        assert_eq!(escrow.logistics_provider, Some(accounts(4)));
    }

    #[test]
    #[should_panic(expected = "logistics provider is fixed before funding")]
    fn precommitted_provider_cannot_be_replaced_after_funding() {
        let mut c = setup();
        let id = create_native_with_provider(&mut c, Some(accounts(4)));
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_yoctonear(115_000_000_000_000_000_000_000_000))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(1)).build());
        c.assign_logistics_provider(id, accounts(3));
    }

    #[test]
    fn creation_tracking_does_not_count_as_dispatch() {
        let mut c = setup();
        let id = create_native_with_provider_and_tracking(
            &mut c,
            Some(accounts(4)),
            Some("DEMO-PRE-1".to_string()),
        );
        assert!(!c.get_escrow(id.clone()).unwrap().shipment_registered);
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_yoctonear(115_000_000_000_000_000_000_000_000))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(1)).build());
        c.assign_logistics_provider(id.clone(), accounts(4));
        let escrow = c.get_escrow(id.clone()).unwrap();
        assert_eq!(escrow.state, EscrowState::ShipmentPending);
        assert!(!escrow.shipment_registered);
        testing_env!(as_account(accounts(2)).block_timestamp(2_000_000_000).build());
        c.refund_buyer(id.clone());
        assert_eq!(c.get_escrow(id).unwrap().state, EscrowState::Refunded);
    }

    #[test]
    #[should_panic(expected = "attach the exact total in yoctoNEAR")]
    fn native_fund_requires_exact_total() {
        let mut c = setup();
        let total = 115_000_000_000_000_000_000_000_000u128;
        let id = create_native(&mut c);
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_yoctonear(total - 1)).build());
        c.fund_escrow(id);
    }

    #[test]
    #[should_panic(expected = "only the preassigned buyer can accept")]
    fn preassigned_escrow_rejects_other_wallets() {
        let mut c = setup();
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        let id = c.create_escrow(
            "Known counterparty".to_string(), "hash".to_string(), U128(100), U128(10),
            Some(accounts(2)), None, "near".parse().unwrap(), 1_000, MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(), None, None,
        );
        // A different wallet tries to accept — the contract refuses.
        testing_env!(as_account(accounts(3)).build());
        c.accept_escrow(id, None);
    }

    #[test]
    fn invite_link_requires_the_secret_and_binds_the_first_signer() {
        let mut c = setup();
        let secret = "high-entropy-invite-secret";
        let hash = sha256_hex(secret.as_bytes());
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        let id = c.create_escrow(
            "Invite link".to_string(), "hash".to_string(), U128(100), U128(10),
            None, Some(hash), "near".parse().unwrap(), 1_000, MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(), None, None,
        );
        testing_env!(as_account(accounts(3)).build());
        let wrong = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            c.accept_escrow(id.clone(), Some("wrong-secret".to_string()));
        }));
        assert!(wrong.is_err(), "wrong invite secret must be rejected");

        c.accept_escrow(id.clone(), Some(secret.to_string()));
        assert_eq!(c.get_escrow(id).unwrap().buyer, Some(accounts(3)));
    }

    #[test]
    #[should_panic(expected = "not a native NEAR escrow")]
    fn native_fund_rejects_ft_escrow() {
        let mut c = setup();
        // Legacy FT is opt-in via the allowlist; otherwise only native NEAR.
        testing_env!(as_account(accounts(1)).build()); // board-only setter
        c.set_allowed_token(Some(accounts(3)));
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        let id = c.create_escrow(
            "Legacy FT".to_string(), "hash".to_string(), U128(100), U128(10), Some(accounts(2)), None,
            accounts(3), 1_000, MIN_INSPECTION_PERIOD_MS, "terms".to_string(), None, None,
        );
        testing_env!(as_account(accounts(2)).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_yoctonear(115)).build());
        c.fund_escrow(id);
    }

    // ------------------------------------------------------------------
    // Permissionless relayer path: verification, logistics, timeout release
    // ------------------------------------------------------------------

    /// Funded + shipped native escrow: seller=accounts(1), buyer=accounts(2),
    /// provider=accounts(4), tracking "DEMO-1".
    fn shipped_native(c: &mut Sweeft) -> String {
        let total = 115_000_000_000_000_000_000_000_000u128;
        let id = create_native(c);
        testing_env!(as_account(accounts(2)).block_timestamp(10_000_000).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .block_timestamp(10_000_000)
            .attached_deposit(NearToken::from_yoctonear(total))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(1)).block_timestamp(20_000_000).build());
        c.assign_logistics_provider(id.clone(), accounts(4));
        testing_env!(as_account(accounts(1)).block_timestamp(30_000_000).build());
        c.register_shipment(id.clone(), "DEMO-1".to_string());
        id
    }

    /// Shipped escrow promoted to the post-verification state directly (the
    /// `#[private]` callback cannot be invoked from a unit test).
    fn delivered_native(c: &mut Sweeft) -> String {
        let id = shipped_native(c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.state = EscrowState::AwaitingBuyerConfirmation;
        escrow.pickup_verified = true;
        escrow.delivered_verified = true;
        c.escrows.insert(id.clone(), escrow);
        c.note_delivery_time(&id);
        id
    }

    fn verification_deposit() -> NearToken {
        NearToken::from_yoctonear(MIN_VERIFICATION_DEPOSIT_YOCTO)
    }

    #[test]
    fn stranger_can_request_delivery_verification() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(5))
            .block_timestamp(40_000_000)
            .attached_deposit(verification_deposit())
            .build());
        c.request_delivery_verification(id.clone(), "{\"status\":\"DELIVERED\"}".to_string());
        assert!(c.get_escrow(id).unwrap().verification_pending);
    }

    #[test]
    #[should_panic(expected = "carrier key not configured")]
    fn verification_requires_a_configured_carrier_key() {
        // Fresh deployments must never run unsigned verification: with no
        // carrier key on-chain, requests revert.
        let mut c = Sweeft::new(
            accounts(0), accounts(5), "test-project".to_string(), 500,
            accounts(1), None, None,
        );
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(5))
            .block_timestamp(40_000_000)
            .attached_deposit(verification_deposit())
            .build());
        c.request_delivery_verification(id, "{}".to_string());
    }

    #[test]
    #[should_panic(expected = "attach at least 0.001 NEAR for OutLayer compute")]
    fn verification_request_still_requires_deposit() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(5)).block_timestamp(40_000_000).build());
        c.request_delivery_verification(id, "{}".to_string());
    }

    #[test]
    #[should_panic(expected = "verification already pending")]
    fn pending_verification_blocks_a_second_request() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(5))
            .block_timestamp(40_000_000)
            .attached_deposit(verification_deposit())
            .build());
        c.request_delivery_verification(id.clone(), "{}".to_string());
        c.request_delivery_verification(id, "{}".to_string());
    }

    #[test]
    fn stranger_can_settle_logistics_after_verified_pickup() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.pickup_verified = true;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.settle_logistics(id.clone());
        assert!(c.get_escrow(id).unwrap().logistics_paid);
    }

    #[test]
    fn inspection_clock_records_only_once() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        let first = c.delivery_time_ms(id.clone()).unwrap();
        assert_eq!(first, 30); // shipped_native's last block timestamp, in ms
        testing_env!(as_account(accounts(0)).block_timestamp(999_000_000).build());
        c.note_delivery_time(&id);
        assert_eq!(c.delivery_time_ms(id).unwrap(), first);
    }

    #[test]
    fn buyer_confirmation_releases_before_the_window() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(40_000_000).build());
        c.confirm_receipt(id.clone());
        let escrow = c.get_escrow(id).unwrap();
        assert!(escrow.seller_paid);
        assert!(escrow.platform_paid);
    }

    #[test]
    fn failed_seller_payout_rolls_back_without_counting_reputation() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(40_000_000).build());
        c.confirm_receipt(id.clone());
        assert_eq!(c.reputation_of(accounts(1)).completed_as_seller, 0);
        assert_eq!(c.reputation_of(accounts(2)).completed_as_buyer, 0);
        c.handle_seller_payout(id.clone(), false);
        assert_eq!(c.get_escrow(id.clone()).unwrap().state, EscrowState::AwaitingBuyerConfirmation);
        assert_eq!(c.reputation_of(accounts(1)).completed_as_seller, 0);
        c.confirm_receipt(id.clone());
        c.handle_seller_payout(id, true);
        assert_eq!(c.reputation_of(accounts(1)).completed_as_seller, 1);
        assert_eq!(c.reputation_of(accounts(2)).completed_as_buyer, 1);
    }

    #[test]
    #[should_panic(expected = "inspection window not elapsed")]
    fn timeout_release_waits_for_the_window() {
        let mut c = setup();
        let id = delivered_native(&mut c); // clock at 30 ms, window 1000 ms
        testing_env!(as_account(accounts(5)).block_timestamp(100_000_000).build()); // 100 ms
        c.release_after_inspection(id);
    }

    #[test]
    fn timeout_release_settles_after_the_window() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(5)).block_timestamp(4_000_000_000_000).build()); // 2000 ms
        c.release_after_inspection(id.clone());
        let escrow = c.get_escrow(id.clone()).unwrap();
        assert!(escrow.seller_paid);
        assert!(escrow.platform_paid);
        assert_eq!(escrow.state, EscrowState::Completed);
        // Reputation waits for the asynchronous seller payout callback.
        assert_eq!(c.reputation_of(accounts(1)).completed_as_seller, 0);
        assert_eq!(c.reputation_of(accounts(2)).completed_as_buyer, 0);
        c.handle_seller_payout(id, true);
        assert_eq!(c.reputation_of(accounts(1)).completed_as_seller, 1);
        assert_eq!(c.reputation_of(accounts(2)).completed_as_buyer, 1);
    }

    #[test]
    #[should_panic(expected = "problem reported")]
    fn timeout_release_blocked_when_problem_reported() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.problem_reported = true;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(5)).block_timestamp(4_000_000_000_000).build());
        c.release_after_inspection(id);
    }

    #[test]
    #[should_panic(expected = "already settled")]
    fn confirmation_and_timeout_release_are_mutually_exclusive() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(5)).block_timestamp(4_000_000_000_000).build());
        c.release_after_inspection(id.clone());
        testing_env!(as_account(accounts(2)).block_timestamp(4_100_000_000_000).build());
        c.confirm_receipt(id);
    }

    // ------------------------------------------------------------------
    // Hardening: storage deposit, caps, pool/board separation, payout safety
    // ------------------------------------------------------------------

    #[test]
    #[should_panic(expected = "attach at least 0.02 NEAR storage deposit")]
    fn creation_requires_a_storage_deposit() {
        let mut c = setup();
        testing_env!(as_account(accounts(1)).block_timestamp(1).build());
        c.create_escrow(
            "No deposit".to_string(), "hash".to_string(), U128(100), U128(10),
            Some(accounts(2)), None, "near".parse().unwrap(), 1_000, MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(), None, None,
        );
    }

    #[test]
    #[should_panic(expected = "product_name too long")]
    fn creation_caps_string_lengths() {
        let mut c = setup();
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.create_escrow(
            "x".repeat(MAX_NAME_LEN + 1), "hash".to_string(), U128(100), U128(10),
            Some(accounts(2)), None, "near".parse().unwrap(), 1_000, MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(), None, None,
        );
    }

    #[test]
    #[should_panic(expected = "token not allowed")]
    fn creation_rejects_unlisted_tokens() {
        let mut c = setup();
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.create_escrow(
            "Arbitrary token".to_string(), "hash".to_string(), U128(100), U128(10),
            Some(accounts(2)), None, accounts(3), 1_000, MIN_INSPECTION_PERIOD_MS,
            "terms".to_string(), None, None,
        );
    }

    #[test]
    #[should_panic(expected = "tracking_number too long")]
    fn shipment_registration_caps_tracking_length() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.state = EscrowState::ShipmentPending;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(1)).build());
        c.register_shipment(id, "x".repeat(MAX_TRACKING_LEN + 1));
    }

    #[test]
    #[should_panic(expected = "reason_hash too long")]
    fn open_dispute_caps_reason_length() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(1)).build());
        c.open_dispute(id, "x".repeat(MAX_HASH_LEN + 1));
    }

    #[test]
    #[should_panic(expected = "reason_hash too long")]
    fn problem_report_caps_reason_length() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(2)).build());
        c.report_problem(id, "x".repeat(MAX_HASH_LEN + 1));
    }

    /// Shipped native escrow escalated by the seller to `ReviewRequired`.
    fn disputed_native(c: &mut Sweeft) -> String {
        let id = shipped_native(c);
        testing_env!(as_account(accounts(1)).block_timestamp(40_000_000).build());
        c.open_dispute(id.clone(), "buyer unresponsive".to_string());
        id
    }

    fn finalized_arbitration_with_stake(c: &mut Sweeft) -> String {
        let id = disputed_native(c);
        gov_config_map().remove(&"arbiter".to_string());
        testing_env!(as_account(accounts(5))
            .attached_deposit(NearToken::from_millinear(50))
            .build());
        c.vote_dispute(id.clone(), 60);
        testing_env!(as_account(accounts(5)).build());
        c.finalize_arbitration(id.clone());
        id
    }

    #[test]
    fn board_resolution_folds_logistics_and_unlocks_stakes() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.resolve_review(id.clone(), 60);
        let escrow = c.get_escrow(id.clone()).unwrap();
        // Logistics was unpaid and is folded into the split — marked paid so it
        // can never be settled twice.
        assert!(escrow.logistics_paid);
        assert_eq!(escrow.state, EscrowState::Refunded);
        // Folded means the FULL 115 is the split pot: buyer due = 40% of 115.
        assert_eq!(review_due(&id), 46_000_000_000_000_000_000_000_000);
    }

    #[test]
    fn dispute_split_with_unpaid_logistics_covers_the_full_total() {
        let mut c = setup();
        let id = disputed_native(&mut c); // logistics NOT paid
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.resolve_review(id.clone(), 60);
        let escrow = c.get_escrow(id.clone()).unwrap();
        let total = escrow.total_amount.0;
        let seller_part = total * 60 / 100;
        let buyer_part = review_due(&id);
        // Every unit of the escrow is allocated: seller + buyer == total.
        assert_eq!(seller_part + buyer_part, total, "all balances must sum to total_amount");
        assert_eq!(buyer_part, total - seller_part);
    }

    #[test]
    fn failed_review_restores_unpaid_logistics_and_full_split_pot() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.resolve_review(id.clone(), 60);
        assert!(c.get_escrow(id.clone()).unwrap().logistics_paid);
        c.handle_resolution_payout(id.clone(), false, "review");
        let escrow = c.get_escrow(id.clone()).unwrap();
        assert_eq!(escrow.state, EscrowState::ReviewRequired);
        assert!(!escrow.logistics_paid);
        assert_eq!(review_due(&id), 0);
        c.resolve_review(id.clone(), 60);
        let total = c.get_escrow(id.clone()).unwrap().total_amount.0;
        assert_eq!(review_due(&id), total * 40 / 100);
    }

    #[test]
    fn failed_review_preserves_already_paid_logistics() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.logistics_paid = true;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.resolve_review(id.clone(), 60);
        c.handle_resolution_payout(id.clone(), false, "review");
        assert!(c.get_escrow(id.clone()).unwrap().logistics_paid);
        c.resolve_review(id.clone(), 60);
        let escrow = c.get_escrow(id.clone()).unwrap();
        let remaining = escrow.total_amount.0 - escrow.logistics_amount.0;
        assert_eq!(review_due(&id), remaining * 40 / 100);
    }

    #[test]
    #[should_panic(expected = "resolution payout pending")]
    fn arbitration_stake_cannot_be_claimed_during_seller_payout() {
        let mut c = setup();
        let id = finalized_arbitration_with_stake(&mut c);
        testing_env!(as_account(accounts(5)).build());
        c.claim_stake(id);
    }

    #[test]
    fn buyer_fallback_refund_unlocks_arbitration_stakes() {
        let mut c = setup();
        let id = finalized_arbitration_with_stake(&mut c);
        c.handle_resolution_payout(id.clone(), false, "arbitration");
        testing_env!(as_account(accounts(2))
            .block_timestamp((40 + DISPUTE_TIMEOUT_MS) * 1_000_000)
            .build());
        c.refund_buyer(id.clone());
        c.handle_payout_result(id.clone(), "refund".to_string(), true);
        testing_env!(as_account(accounts(5)).build());
        c.claim_stake(id.clone());
        let case = c.arbitration_status(id).unwrap();
        assert!(case.finalized);
        assert!(case.votes[0].claimed);
    }

    #[test]
    #[should_panic(expected = "logistics already paid")]
    fn logistics_cannot_be_paid_again_after_resolution() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.pickup_verified = true; // would qualify for logistics payout
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(1)).block_timestamp(50_000_000).build());
        c.resolve_review(id.clone(), 60);
        testing_env!(as_account(accounts(1)).block_timestamp(60_000_000).build());
        c.settle_logistics(id);
    }

    #[test]
    #[should_panic(expected = "escrow not in a settleable state")]
    fn logistics_cannot_settle_in_review_state() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.pickup_verified = true;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(1)).block_timestamp(60_000_000).build());
        c.settle_logistics(id);
    }

    fn outcome(escrow_id: &str, tracking: &str, provider_id: &str) -> VerificationOutcome {
        VerificationOutcome {
            escrow_id: escrow_id.to_string(),
            tracking_number: tracking.to_string(),
            provider_id: provider_id.to_string(),
            reported_status: "DELIVERED".to_string(),
            verified: true,
            reason: "test".to_string(),
        }
    }

    #[test]
    fn callback_rejects_tracking_mismatch() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        // Pretend the OutLayer callback runs on the contract's own account.
        testing_env!(as_account(accounts(0))
            .current_account_id(accounts(0))
            .block_timestamp(40_000_000)
            .build());
        let out = c.on_verification_result(id.clone(), Ok(Some(outcome(&id, "WRONG-TRACK", "LOG-DEMO-001"))));
        // The rejection is recorded as evidence (verified=false), never applied.
        assert!(out.is_some());
        assert!(!out.unwrap().verified);
        let escrow = c.get_escrow(id).unwrap();
        assert!(!escrow.delivered_verified, "mismatched tracking must not verify");
        assert_eq!(escrow.state, EscrowState::Shipped);
    }

    #[test]
    fn callback_rejects_provider_mismatch_when_configured() {
        let mut c = setup();
        // provider id is already set at init (board config)
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(0))
            .current_account_id(accounts(0))
            .block_timestamp(40_000_000)
            .build());
        let escrow = c.get_escrow(id.clone()).unwrap();
        let track = escrow.tracking_number.clone().unwrap();
        let out = c.on_verification_result(id.clone(), Ok(Some(outcome(&id, &track, "LOG-EVIL-999"))));
        // Outcome is returned but invalidated: verified false, no state advance.
        assert!(out.is_some());
        assert!(!out.unwrap().verified);
        let escrow = c.get_escrow(id).unwrap();
        assert!(!escrow.delivered_verified);
        assert_eq!(escrow.state, EscrowState::Shipped);
    }

    #[test]
    fn completion_pays_logistics_when_unpaid() {
        let mut c = setup();
        let id = delivered_native(&mut c); // logistics NOT settled yet
        testing_env!(as_account(accounts(2)).block_timestamp(40_000_000).build());
        c.confirm_receipt(id.clone());
        let escrow = c.get_escrow(id).unwrap();
        // Regression: completion must pay (or fold) logistics — never strand it.
        assert!(escrow.logistics_paid, "completion must not strand logistics");
        assert!(escrow.seller_paid);
        assert!(escrow.platform_paid);
        assert_eq!(escrow.state, EscrowState::Completed);
    }

    #[test]
    fn timeout_release_also_pays_logistics_when_unpaid() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(5)).block_timestamp(4_000_000_000_000).build());
        c.release_after_inspection(id.clone());
        let escrow = c.get_escrow(id).unwrap();
        assert!(escrow.logistics_paid, "timeout release must not strand logistics");
        assert!(escrow.seller_paid);
        assert!(escrow.platform_paid);
    }

    #[test]
    #[should_panic(expected = "assign a logistics provider before completion")]
    fn completion_requires_provider_when_logistics_unpaid() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        let mut escrow = c.get_escrow(id.clone()).unwrap();
        escrow.logistics_provider = None;
        c.escrows.insert(id.clone(), escrow);
        testing_env!(as_account(accounts(2)).block_timestamp(40_000_000).build());
        c.confirm_receipt(id);
    }

    #[test]
    #[should_panic(expected = "only the board can set the carrier key")]
    fn owner_cannot_rotate_carrier_key() {
        let mut c = setup();
        c.set_carrier_pubkey(Some("ab".repeat(32)));
    }

    #[test]
    fn board_can_rotate_carrier_key() {
        let mut c = setup();
        testing_env!(as_account(accounts(1)).build());
        c.set_carrier_pubkey(Some("ab".repeat(32)));
        assert_eq!(c.get_carrier_pubkey(), Some("ab".repeat(32)));
    }

    #[test]
    #[should_panic(expected = "only the board can set the provider id")]
    fn owner_cannot_set_provider_id() {
        let mut c = setup();
        c.set_provider_id(Some("LOG-X".to_string()));
    }

    #[test]
    fn long_stop_refunds_the_buyer_after_grace() {
        let mut c = setup();
        let id = shipped_native(&mut c); // shipment registered, never delivered
        testing_env!(as_account(accounts(2))
            .block_timestamp(2_592_001_001_000_000) // deadline (1s) + 30d + 1s
            .build());
        c.refund_buyer(id.clone());
        let escrow = c.get_escrow(id).unwrap();
        assert_eq!(escrow.state, EscrowState::Refunded);
        // Logistics was never paid → full total is refunded.
        assert!(!escrow.logistics_paid);
    }

    #[test]
    #[should_panic(expected = "refund not available yet")]
    fn long_stop_blocked_before_grace() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(2_000_000_000).build());
        c.refund_buyer(id);
    }

    #[test]
    fn no_dispatch_refund_still_works() {
        let mut c = setup();
        let id = create_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(10_000_000).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .block_timestamp(10_000_000)
            .attached_deposit(NearToken::from_yoctonear(115_000_000_000_000_000_000_000_000))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(2)).block_timestamp(2_000_000_000).build());
        c.refund_buyer(id.clone());
        assert_eq!(c.get_escrow(id).unwrap().state, EscrowState::Refunded);
    }

    #[test]
    fn seller_dispute_cannot_block_no_dispatch_refund() {
        let mut c = setup();
        let id = create_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(10_000_000).build());
        c.accept_escrow(id.clone(), None);
        testing_env!(as_account(accounts(2))
            .block_timestamp(10_000_000)
            .attached_deposit(NearToken::from_yoctonear(115_000_000_000_000_000_000_000_000))
            .build());
        c.fund_escrow(id.clone());
        testing_env!(as_account(accounts(1)).block_timestamp(20_000_000).build());
        c.open_dispute(id.clone(), "not shipping".to_string());
        assert_eq!(c.dispute_time_ms(id.clone()), Some(20));
        testing_env!(as_account(accounts(2)).block_timestamp(2_000_000_000).build());
        c.refund_buyer(id.clone());
        assert_eq!(c.get_escrow(id.clone()).unwrap().state, EscrowState::Refunded);
        c.handle_payout_result(id.clone(), "refund".to_string(), false);
        assert_eq!(c.get_escrow(id.clone()).unwrap().state, EscrowState::ReviewRequired);
        assert_eq!(c.reputation_of(accounts(2)).refunds_claimed, 0);
        c.refund_buyer(id.clone());
        c.handle_payout_result(id, "refund".to_string(), true);
        assert_eq!(c.reputation_of(accounts(2)).refunds_claimed, 1);
    }

    #[test]
    fn unresolved_dispute_defaults_to_buyer_refund_after_seven_days() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        testing_env!(as_account(accounts(2))
            .block_timestamp((40 + DISPUTE_TIMEOUT_MS) * 1_000_000)
            .build());
        c.refund_buyer(id.clone());
        assert_eq!(c.get_escrow(id).unwrap().state, EscrowState::Refunded);
    }

    #[test]
    #[should_panic(expected = "refund not available yet")]
    fn dispute_refund_waits_for_seven_day_deadline() {
        let mut c = setup();
        let id = disputed_native(&mut c);
        testing_env!(as_account(accounts(2))
            .block_timestamp((40 + DISPUTE_TIMEOUT_MS - 1) * 1_000_000)
            .build());
        c.refund_buyer(id);
    }

    #[test]
    fn failed_long_stop_refund_restores_shipped_state() {
        let mut c = setup();
        let id = shipped_native(&mut c);
        testing_env!(as_account(accounts(2))
            .block_timestamp(2_592_001_001_000_000)
            .build());
        c.refund_buyer(id.clone());
        c.handle_payout_result(id.clone(), "refund".to_string(), false);
        assert_eq!(c.get_escrow(id.clone()).unwrap().state, EscrowState::Shipped);
        assert_eq!(c.reputation_of(accounts(2)).refunds_claimed, 0);
        c.refund_buyer(id.clone());
        c.handle_payout_result(id, "refund".to_string(), true);
        assert_eq!(c.reputation_of(accounts(2)).refunds_claimed, 1);
    }

    #[test]
    #[should_panic(expected = "inspection period below the 1 hour floor")]
    fn inspection_period_floor_enforced() {
        let mut c = setup();
        testing_env!(as_account(accounts(1))
            .block_timestamp(1)
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.create_escrow(
            "Floor".to_string(), "hash".to_string(), U128(100), U128(10),
            Some(accounts(2)), None, "near".parse().unwrap(), 1_000, 1_000,
            "terms".to_string(), None, None,
        );
    }

    #[test]
    #[should_panic(expected = "already settled")]
    fn confirm_receipt_is_exactly_once() {
        let mut c = setup();
        let id = delivered_native(&mut c);
        testing_env!(as_account(accounts(2)).block_timestamp(40_000_000).build());
        c.confirm_receipt(id.clone());
        c.confirm_receipt(id);
    }
}
