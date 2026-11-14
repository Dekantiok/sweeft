//! Sweeft co-pilot budget — native-NEAR allowance for agent payments.
//!
//! A user deposits exactly the amount they allow the AI co-pilot to spend.
//! Only an authorized agent account may call `pay`; every payment is capped
//! by `allowance - spent`. The user can update the limit with another deposit
//! or revoke instantly by withdrawing the unspent remainder.
//!
//! Two hardening layers sit on top of that:
//!
//! * **Owner-scoped agent authorization.** Any depositor can call
//!   `set_my_agent(Some(agent))` to appoint an agent for its own budget, or
//!   `set_my_agent(None)` to revoke all spending from its budget. Owners that
//!   never call it keep using the global agent configured at deploy time.
//! * **Payment idempotency.** Every `pay` call carries a caller-supplied
//!   `intent_id`. The `(owner, intent_id)` pair is recorded before the
//!   transfer is scheduled, so a duplicated call is rejected; if the transfer
//!   fails the mark is removed again and a retry may reuse the same
//!   identifier. Successful payments stay marked, so a transfer that went
//!   through can never execute twice.
//!
//! Both layers live in out-of-struct `LookupMap`s with their own storage
//! prefixes (`oa1`, `rv1`, `pi1`), so the deployed `NearBudget` state struct
//! and its Borsh layout — and therefore every existing budget — is untouched
//! by an upgrade.
//!
//! This is the native-NEAR replacement for the old NEP-141
//! `approve`/`ft_transfer_from` allowance: same scoped, revocable security
//! boundary, but judges need only the NEAR they already hold.

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::json_types::U128;
use near_sdk::serde::{Deserialize, Serialize};
use near_sdk::store::LookupMap;
use near_sdk::{env, near, require, AccountId, NearToken, PanicOnDefault, Promise, PromiseResult};

/// Default per-payment cap: 0.1 NEAR in yoctoNEAR (owner can raise/lower).
const DEFAULT_MAX_PAYMENT: u128 = 100_000_000_000_000_000_000_000;

/// Longest accepted `intent_id` (bytes). Ids are caller-supplied, so they are
/// bounded to keep payment metadata small and predictable.
const MAX_INTENT_ID_LEN: usize = 128;

/// Config lives under its own prefix (never a state-struct field) so upgrades
/// of an initialized contract need no migration.
fn cfg_map() -> LookupMap<String, U128> {
    LookupMap::new(b"cfg1".to_vec())
}

/// `owner -> owner-scoped agent`. New prefix: never a `NearBudget` struct
/// field, so the struct's Borsh layout (and existing balances) is unchanged.
fn owner_agents_map() -> LookupMap<AccountId, AccountId> {
    LookupMap::new(b"oa1".to_vec())
}

/// `owner -> revoked`. Explicit revocation sentinel: `true` means no agent —
/// not even the global one — may spend from this owner's budget until the
/// owner authorizes an agent again.
fn revoked_map() -> LookupMap<AccountId, bool> {
    LookupMap::new(b"rv1".to_vec())
}

/// `(owner, intent_id) -> consumed`. Keyed map, so a duplicate check is O(1)
/// with no per-owner list to overflow. Entries for successful payments are
/// kept so the transfer can never execute twice; a failed transfer removes
/// its entry so the same identifier may be retried.
fn payment_intents_map() -> LookupMap<(AccountId, String), bool> {
    LookupMap::new(b"pi1".to_vec())
}

#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug, Default)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct BudgetEntry {
    /// Current authorized ceiling. Deposits increase it, withdrawals decrease it.
    pub allowance: U128,
    /// Lifetime amount the agent has spent from this owner's budget.
    pub spent: U128,
}

#[derive(BorshDeserialize, BorshSerialize, Serialize, Deserialize, Clone, Debug)]
#[borsh(crate = "near_sdk::borsh")]
#[serde(crate = "near_sdk::serde")]
pub struct BudgetView {
    pub allowance: U128,
    pub spent: U128,
    pub available: U128,
}

/// NOTE: this struct's fields and their order define the deployed Borsh state
/// layout. Do not add or reorder fields here; new state goes into the
/// out-of-struct `LookupMap`s above (new prefixes) so upgrades need no
/// migration and existing budgets stay readable.
#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct NearBudget {
    owner: AccountId,
    agent: AccountId,
    budgets: LookupMap<AccountId, BudgetEntry>,
}

#[near]
impl NearBudget {
    #[init]
    pub fn new(agent: AccountId) -> Self {
        Self {
            owner: env::predecessor_account_id(),
            agent,
            budgets: LookupMap::new(b"b".to_vec()),
        }
    }

    fn available(entry: &BudgetEntry) -> u128 {
        entry.allowance.0.saturating_sub(entry.spent.0)
    }

    /// Whether `owner` explicitly revoked agent spending from its budget.
    pub fn is_revoked(&self, owner: AccountId) -> bool {
        revoked_map().get(&owner).copied().unwrap_or(false)
    }

    /// The effective agent authorized to spend from `owner`'s budget, if any:
    ///
    /// * `Some(owner_agent)` when the owner set a per-owner agent and has not
    ///   revoked it (this overrides the global agent),
    /// * `None` when the owner revoked (`set_my_agent(None)`) — nobody may
    ///   spend,
    /// * `Some(global_agent)` when the owner never set a per-owner agent.
    ///
    /// `pay` enforces exactly these rules, so clients never have to rely on
    /// this view for safety.
    pub fn get_owner_agent(&self, owner: AccountId) -> Option<AccountId> {
        if self.is_revoked(owner.clone()) {
            return None;
        }
        if let Some(agent) = owner_agents_map().get(&owner) {
            return Some(agent.clone());
        }
        Some(self.agent.clone())
    }

    /// Caller-scoped agent authorization.
    ///
    /// `Some(agent)` authorizes exactly `agent` to spend from the caller's
    /// budget and clears any previous revocation. `None` records an explicit
    /// revocation: no agent (not even the global one) can spend from the
    /// caller's budget until the caller authorizes an agent again.
    pub fn set_my_agent(&mut self, agent: Option<AccountId>) {
        let caller = env::predecessor_account_id();
        match agent {
            Some(agent) => {
                owner_agents_map().insert(caller.clone(), agent);
                revoked_map().insert(caller, false);
            }
            None => {
                revoked_map().insert(caller, true);
            }
        }
    }

    /// Deposit native NEAR as the caller's agent-spending limit. Attach the
    /// exact amount the co-pilot may spend (additional deposits raise it).
    #[payable]
    pub fn deposit(&mut self) {
        let account = env::predecessor_account_id();
        let amount = env::attached_deposit().as_yoctonear();
        require!(amount > 0, "attach NEAR to set a budget");
        let mut entry = self.budgets.get(&account).cloned().unwrap_or_default();
        entry.allowance.0 = entry
            .allowance
            .0
            .checked_add(amount)
            .expect("allowance overflow");
        self.budgets.insert(account, entry);
    }

    /// Withdraw unspent budget. Setting `amount` to the full available value
    /// is the one-call revoke: the agent can no longer spend anything.
    pub fn withdraw(&mut self, amount: U128) -> Promise {
        let account = env::predecessor_account_id();
        let mut entry = self
            .budgets
            .get(&account)
            .cloned()
            .expect("no budget for this account");
        require!(amount.0 > 0, "amount must be > 0");
        require!(
            Self::available(&entry) >= amount.0,
            "not enough unspent budget"
        );
        entry.allowance.0 -= amount.0;
        self.budgets.insert(account.clone(), entry);
        Promise::new(account.clone())
            .transfer(NearToken::from_yoctonear(amount.0))
            .then(Self::ext(env::current_account_id()).on_withdraw_result(account, amount))
    }

    #[private]
    pub fn on_withdraw_result(&mut self, account: AccountId, amount: U128) {
        if matches!(env::promise_result(0), near_sdk::PromiseResult::Failed) {
            let mut entry = self.budgets.get(&account).cloned().unwrap_or_default();
            entry.allowance.0 += amount.0;
            self.budgets.insert(account, entry);
        }
    }

    /// On-chain per-payment cap (yoctoNEAR). Server-side settings alone are
    /// not a trust boundary; this one is enforced by the contract itself.
    pub fn set_max_payment(&mut self, amount: U128) {
        require!(
            env::predecessor_account_id() == self.owner,
            "only the budget owner"
        );
        cfg_map().insert("max_payment".to_string(), amount);
    }

    pub fn get_max_payment(&self) -> U128 {
        cfg_map()
            .get(&"max_payment".to_string())
            .copied()
            .unwrap_or(U128(DEFAULT_MAX_PAYMENT))
    }

    /// Agent-only payment from `owner`'s remaining allowance to `receiver`.
    ///
    /// Authorization: the predecessor must be `owner`'s effective agent —
    /// the owner-scoped agent when one is set and not revoked, otherwise the
    /// global agent. A revoked owner rejects every agent.
    ///
    /// Idempotency: `(owner, intent_id)` is consumed before the transfer is
    /// scheduled, so a concurrent/duplicate call with the same identifier is
    /// rejected ("intent already processed") — NEAR executes the receipts of
    /// one contract sequentially, and the mark is written before the transfer
    /// promise is created. The spend is recorded before the transfer and, if
    /// the transfer fails, `on_pay_result` rolls both the spend and the intent
    /// mark back so a retry may reuse the original identifier. On success the
    /// mark stays: a successful transfer can never execute twice.
    pub fn pay(
        &mut self,
        owner: AccountId,
        receiver: AccountId,
        amount: U128,
        intent_id: String,
    ) -> Promise {
        require!(
            !self.is_revoked(owner.clone()),
            "agent authorization revoked by owner"
        );
        let predecessor = env::predecessor_account_id();
        require!(
            self.get_owner_agent(owner.clone()) == Some(predecessor),
            "only the co-pilot agent can pay"
        );
        require!(amount.0 > 0, "amount must be > 0");
        require!(
            amount.0 <= self.get_max_payment().0,
            "above the on-chain per-payment cap"
        );
        require!(!intent_id.is_empty(), "intent_id required");
        require!(intent_id.len() <= MAX_INTENT_ID_LEN, "intent_id too long");

        let intent_key = (owner.clone(), intent_id.clone());
        let mut intents = payment_intents_map();
        require!(
            !intents.contains_key(&intent_key),
            "intent already processed"
        );

        let mut entry = self
            .budgets
            .get(&owner)
            .cloned()
            .expect("no budget for this owner");
        require!(Self::available(&entry) >= amount.0, "budget exceeded");

        // Consume the intent and record the spend before scheduling the
        // transfer. Everything that can reject the call has already run, and
        // receipt execution for one contract is sequential, so no duplicate
        // can slip through between this check and the promise creation.
        intents.insert(intent_key, true);
        intents.flush();
        entry.spent.0 = entry.spent.0.checked_add(amount.0).expect("spent overflow");
        self.budgets.insert(owner.clone(), entry);

        Promise::new(receiver)
            .transfer(NearToken::from_yoctonear(amount.0))
            .then(Self::ext(env::current_account_id()).on_pay_result(owner, amount, intent_id))
    }

    /// Rollback callback for `pay`. On transfer failure the spend accounting
    /// is reversed and the `(owner, intent_id)` mark is cleared so the caller
    /// can retry with the same identifier. On success nothing is done: the
    /// intent stays consumed.
    #[private]
    pub fn on_pay_result(&mut self, owner: AccountId, amount: U128, intent_id: String) {
        if matches!(env::promise_result(0), PromiseResult::Failed) {
            self.rollback_payment(owner, amount, intent_id);
        }
    }

    /// Reverse a payment whose transfer never completed: give the amount back
    /// to the owner's available budget and release the intent mark.
    fn rollback_payment(&mut self, owner: AccountId, amount: U128, intent_id: String) {
        let mut entry = self.budgets.get(&owner).cloned().unwrap_or_default();
        entry.spent.0 = entry.spent.0.saturating_sub(amount.0);
        self.budgets.insert(owner.clone(), entry);
        payment_intents_map().remove(&(owner, intent_id));
    }

    /// Owner (deployer) can rotate the agent account if operations require it.
    pub fn set_agent(&mut self, agent: AccountId) {
        require!(
            env::predecessor_account_id() == self.owner,
            "only the budget owner"
        );
        self.agent = agent;
    }

    pub fn get_agent(&self) -> AccountId {
        self.agent.clone()
    }

    pub fn budget_of(&self, owner: AccountId) -> BudgetView {
        let entry = self.budgets.get(&owner).cloned().unwrap_or_default();
        BudgetView {
            allowance: entry.allowance,
            spent: entry.spent,
            available: U128(Self::available(&entry)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use near_sdk::test_utils::{accounts, VMContextBuilder};
    use near_sdk::testing_env;
    use std::collections::HashMap;

    fn as_account(account: AccountId) -> VMContextBuilder {
        let mut b = VMContextBuilder::new();
        b.predecessor_account_id(account)
            .account_balance(NearToken::from_near(100));
        b
    }

    /// Mock environment carrying a promise result, for exercising callbacks
    /// (`env::promise_result(0)`) without faking success.
    fn env_with_promise_result(caller: AccountId, result: PromiseResult) {
        testing_env!(
            as_account(caller).build(),
            near_sdk::test_vm_config(),
            near_sdk::RuntimeFeesConfig::test(),
            HashMap::default(),
            vec![result],
        );
    }

    fn setup() -> NearBudget {
        testing_env!(as_account(accounts(0)).build());
        let mut c = NearBudget::new(accounts(1));
        // Most tests exercise budgets, not the cap; lift it out of the way.
        c.set_max_payment(U128(100 * 10u128.pow(24)));
        c
    }

    #[test]
    fn deposit_spend_and_available() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(2))
            .build());
        c.deposit();
        assert_eq!(c.budget_of(accounts(2)).available.0, 2 * 10u128.pow(24));

        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(3),
            U128(5 * 10u128.pow(23)),
            "i-1".to_string(),
        );
        let view = c.budget_of(accounts(2));
        assert_eq!(view.spent.0, 5 * 10u128.pow(23));
        assert_eq!(view.available.0, 15 * 10u128.pow(23));
    }

    #[test]
    #[should_panic(expected = "only the co-pilot agent can pay")]
    fn stranger_cannot_spend() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(3)).build());
        let _ = c.pay(accounts(2), accounts(4), U128(1), "s-1".to_string());
    }

    #[test]
    #[should_panic(expected = "budget exceeded")]
    fn cannot_exceed_budget() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(3),
            U128(2 * 10u128.pow(24)),
            "b-1".to_string(),
        );
    }

    #[test]
    fn withdraw_revokes_unspent_budget() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(3),
            U128(4 * 10u128.pow(23)),
            "w-1".to_string(),
        );
        testing_env!(as_account(accounts(2)).build());
        let _ = c.withdraw(U128(6 * 10u128.pow(23)));
        let view = c.budget_of(accounts(2));
        assert_eq!(view.available.0, 0);
        assert_eq!(view.spent.0, 4 * 10u128.pow(23));
    }

    #[test]
    fn additional_deposit_raises_limit() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        assert_eq!(c.budget_of(accounts(2)).available.0, 2 * 10u128.pow(24));
    }

    #[test]
    fn on_chain_per_payment_cap_is_enforced() {
        let mut c = setup();
        // Back to the default cap for this test.
        testing_env!(as_account(accounts(0)).build());
        c.set_max_payment(U128(DEFAULT_MAX_PAYMENT));
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        assert_eq!(c.get_max_payment().0, DEFAULT_MAX_PAYMENT);
        testing_env!(as_account(accounts(1)).build());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(
                accounts(2),
                accounts(3),
                U128(DEFAULT_MAX_PAYMENT + 1),
                "cap-1".to_string(),
            );
        }));
        assert!(result.is_err(), "payment above the on-chain cap must fail");
        // Owner can adjust the cap.
        testing_env!(as_account(accounts(0)).build());
        c.set_max_payment(U128(DEFAULT_MAX_PAYMENT * 2));
        assert_eq!(c.get_max_payment().0, DEFAULT_MAX_PAYMENT * 2);
    }

    #[test]
    #[should_panic(expected = "only the budget owner")]
    fn non_owner_cannot_change_cap() {
        let mut c = setup();
        testing_env!(as_account(accounts(3)).build());
        c.set_max_payment(U128(1));
    }

    // -------------------------------------------------------------------
    // Owner-scoped agent authorization
    // -------------------------------------------------------------------

    #[test]
    fn owner_scoped_agent_can_spend() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(2))
            .build());
        c.deposit();
        c.set_my_agent(Some(accounts(5)));
        assert_eq!(c.get_owner_agent(accounts(2)), Some(accounts(5)));
        assert!(!c.is_revoked(accounts(2)));

        testing_env!(as_account(accounts(5)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(5 * 10u128.pow(23)),
            "op-1".to_string(),
        );
        let view = c.budget_of(accounts(2));
        assert_eq!(view.spent.0, 5 * 10u128.pow(23));
        assert_eq!(view.available.0, 15 * 10u128.pow(23));
    }

    #[test]
    fn global_agent_fallback_when_owner_sets_nothing() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        // No per-owner agent: the global agent applies.
        assert_eq!(c.get_owner_agent(accounts(2)), Some(accounts(1)));
        assert!(!c.is_revoked(accounts(2)));

        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "gf-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));
    }

    #[test]
    #[should_panic(expected = "only the co-pilot agent can pay")]
    fn global_agent_cannot_spend_when_owner_appointed_another() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        c.set_my_agent(Some(accounts(5)));
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "ga-1".to_string(),
        );
    }

    #[test]
    #[should_panic(expected = "only the co-pilot agent can pay")]
    fn another_owners_agent_cannot_spend() {
        let mut c = setup();
        // Owner accounts(2) authorizes accounts(5).
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        c.set_my_agent(Some(accounts(5)));
        // Owner accounts(3) deposits but keeps the global fallback.
        testing_env!(as_account(accounts(3))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        // accounts(5) is not authorized for accounts(3)'s budget.
        testing_env!(as_account(accounts(5)).build());
        let _ = c.pay(
            accounts(3),
            accounts(4),
            U128(10u128.pow(23)),
            "other-1".to_string(),
        );
    }

    #[test]
    fn set_my_agent_is_caller_scoped() {
        let mut c = setup();
        testing_env!(as_account(accounts(2)).build());
        c.set_my_agent(Some(accounts(5)));
        assert_eq!(c.get_owner_agent(accounts(2)), Some(accounts(5)));
        // Owners that never called it are untouched.
        assert_eq!(c.get_owner_agent(accounts(3)), Some(accounts(1)));
        assert!(!c.is_revoked(accounts(3)));
    }

    #[test]
    fn revoke_blocks_spending_and_reauthorizing_restores() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        c.set_my_agent(Some(accounts(5)));
        assert_eq!(c.get_owner_agent(accounts(2)), Some(accounts(5)));

        // Revoke: the owner now has no effective agent at all.
        c.set_my_agent(None);
        assert!(c.is_revoked(accounts(2)));
        assert_eq!(c.get_owner_agent(accounts(2)), None);

        // Neither the global agent nor the previously appointed one can spend.
        testing_env!(as_account(accounts(1)).build());
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(
                accounts(2),
                accounts(4),
                U128(10u128.pow(23)),
                "rev-1".to_string(),
            );
        }));
        assert!(r.is_err(), "revoked owner must reject the global agent");

        testing_env!(as_account(accounts(5)).build());
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(
                accounts(2),
                accounts(4),
                U128(10u128.pow(23)),
                "rev-1".to_string(),
            );
        }));
        assert!(r.is_err(), "revoked owner must reject the per-owner agent");

        // Revocation is per-owner: other owners keep the global agent.
        testing_env!(as_account(accounts(3))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(3),
            accounts(4),
            U128(10u128.pow(23)),
            "rev-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(3)).spent.0, 10u128.pow(23));

        // Re-authorizing clears the revocation and restores spending.
        testing_env!(as_account(accounts(2)).build());
        c.set_my_agent(Some(accounts(5)));
        assert!(!c.is_revoked(accounts(2)));
        testing_env!(as_account(accounts(5)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "rev-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));
    }

    #[test]
    #[should_panic(expected = "agent authorization revoked by owner")]
    fn revoked_owner_panics_with_clear_message() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        c.set_my_agent(None);
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(accounts(2), accounts(4), U128(1), "rev-2".to_string());
    }

    // -------------------------------------------------------------------
    // Payment idempotency
    // -------------------------------------------------------------------

    #[test]
    #[should_panic(expected = "intent already processed")]
    fn duplicate_intent_is_rejected() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(accounts(2), accounts(4), U128(1), "dup-1".to_string());
        let _ = c.pay(accounts(2), accounts(4), U128(1), "dup-1".to_string());
    }

    #[test]
    fn intent_namespace_is_per_owner_and_per_intent() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(3))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();

        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "shared".to_string(),
        );

        // A different intent for the same owner is fine.
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "other".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 2 * 10u128.pow(23));

        // The same intent for a different owner is fine (key includes owner).
        let _ = c.pay(
            accounts(3),
            accounts(4),
            U128(10u128.pow(23)),
            "shared".to_string(),
        );
        assert_eq!(c.budget_of(accounts(3)).spent.0, 10u128.pow(23));

        // The same (owner, intent) again is rejected.
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(accounts(2), accounts(4), U128(1), "shared".to_string());
        }));
        assert!(r.is_err(), "duplicate (owner, intent) must be rejected");
    }

    #[test]
    fn intent_id_bounds_are_enforced() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());

        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(accounts(2), accounts(4), U128(1), String::new());
        }));
        assert!(r.is_err(), "empty intent id must be rejected");

        let too_long = "x".repeat(MAX_INTENT_ID_LEN + 1);
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(accounts(2), accounts(4), U128(1), too_long);
        }));
        assert!(r.is_err(), "over-long intent id must be rejected");

        // The maximum length is accepted.
        let max_len = "x".repeat(MAX_INTENT_ID_LEN);
        let _ = c.pay(accounts(2), accounts(4), U128(1), max_len);
    }

    #[test]
    fn failed_transfer_rolls_back_and_allows_retry() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();

        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "retry-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));

        // The transfer receipt failed: run the real rollback path with an
        // injected failed promise result (the unit-test VM supports this).
        env_with_promise_result(accounts(0), PromiseResult::Failed);
        c.on_pay_result(accounts(2), U128(10u128.pow(23)), "retry-1".to_string());
        assert_eq!(c.budget_of(accounts(2)).spent.0, 0);

        // The intent mark was released, so a retry with the SAME id works.
        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "retry-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));
    }

    #[test]
    fn successful_transfer_keeps_intent_consumed() {
        let mut c = setup();
        testing_env!(as_account(accounts(2))
            .attached_deposit(NearToken::from_near(1))
            .build());
        c.deposit();

        testing_env!(as_account(accounts(1)).build());
        let _ = c.pay(
            accounts(2),
            accounts(4),
            U128(10u128.pow(23)),
            "ok-1".to_string(),
        );
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));

        // Success callback: spent stays and the intent mark is kept.
        env_with_promise_result(accounts(0), PromiseResult::Successful(vec![]));
        c.on_pay_result(accounts(2), U128(10u128.pow(23)), "ok-1".to_string());
        assert_eq!(c.budget_of(accounts(2)).spent.0, 10u128.pow(23));

        // A replay after success is still rejected.
        testing_env!(as_account(accounts(1)).build());
        let r = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            let _ = c.pay(
                accounts(2),
                accounts(4),
                U128(10u128.pow(23)),
                "ok-1".to_string(),
            );
        }));
        assert!(r.is_err(), "successful transfer must never execute twice");
    }
}
