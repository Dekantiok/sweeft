//! VeriPay co-pilot budget — native-NEAR allowance for agent payments.
//!
//! A user deposits exactly the amount they allow the AI co-pilot to spend.
//! Only the configured agent account may call `pay`; every payment is capped
//! by `allowance - spent`. The user can update the limit with another deposit
//! or revoke instantly by withdrawing the unspent remainder.
//!
//! This is the native-NEAR replacement for the old NEP-141
//! `approve`/`ft_transfer_from` allowance: same scoped, revocable security
//! boundary, but judges need only the NEAR they already hold.

use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::json_types::U128;
use near_sdk::serde::{Deserialize, Serialize};
use near_sdk::store::LookupMap;
use near_sdk::{env, near, require, AccountId, NearToken, PanicOnDefault, Promise};

/// Default per-payment cap: 0.1 NEAR in yoctoNEAR (owner can raise/lower).
const DEFAULT_MAX_PAYMENT: u128 = 100_000_000_000_000_000_000_000;

/// Config lives under its own prefix (never a state-struct field) so upgrades
/// of an initialized contract need no migration.
fn cfg_map() -> near_sdk::store::LookupMap<String, U128> {
    near_sdk::store::LookupMap::new(b"cfg1".to_vec())
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
        let mut entry = self.budgets.get(&account).cloned().expect("no budget for this account");
        require!(amount.0 > 0, "amount must be > 0");
        require!(Self::available(&entry) >= amount.0, "not enough unspent budget");
        entry.allowance.0 -= amount.0;
        self.budgets.insert(account.clone(), entry);
        Promise::new(account.clone())
            .transfer(NearToken::from_yoctonear(amount.0))
            .then(
                Self::ext(env::current_account_id()).on_withdraw_result(account, amount),
            )
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
        require!(env::predecessor_account_id() == self.owner, "only the budget owner");
        cfg_map().insert("max_payment".to_string(), amount);
    }

    pub fn get_max_payment(&self) -> U128 {
        cfg_map()
            .get(&"max_payment".to_string())
            .copied()
            .unwrap_or(U128(DEFAULT_MAX_PAYMENT))
    }

    /// Agent-only payment from `owner`'s remaining allowance to `receiver`.
    /// Spent is recorded before the transfer and rolled back by the callback
    /// if the NEAR transfer fails.
    pub fn pay(&mut self, owner: AccountId, receiver: AccountId, amount: U128) -> Promise {
        require!(env::predecessor_account_id() == self.agent, "only the co-pilot agent can pay");
        require!(amount.0 > 0, "amount must be > 0");
        require!(amount.0 <= self.get_max_payment().0, "above the on-chain per-payment cap");
        let mut entry = self.budgets.get(&owner).cloned().expect("no budget for this owner");
        require!(Self::available(&entry) >= amount.0, "budget exceeded");
        entry.spent.0 += amount.0;
        self.budgets.insert(owner.clone(), entry);
        Promise::new(receiver)
            .transfer(NearToken::from_yoctonear(amount.0))
            .then(Self::ext(env::current_account_id()).on_pay_result(owner, amount))
    }

    #[private]
    pub fn on_pay_result(&mut self, owner: AccountId, amount: U128) {
        if matches!(env::promise_result(0), near_sdk::PromiseResult::Failed) {
            let mut entry = self.budgets.get(&owner).cloned().unwrap_or_default();
            entry.spent.0 = entry.spent.0.saturating_sub(amount.0);
            self.budgets.insert(owner, entry);
        }
    }

    /// Owner (deployer) can rotate the agent account if operations require it.
    pub fn set_agent(&mut self, agent: AccountId) {
        require!(env::predecessor_account_id() == self.owner, "only the budget owner");
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

    fn as_account(account: AccountId) -> VMContextBuilder {
        let mut b = VMContextBuilder::new();
        b.predecessor_account_id(account)
            .account_balance(NearToken::from_near(100));
        b
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
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(2)).build());
        c.deposit();
        assert_eq!(c.budget_of(accounts(2)).available.0, 2 * 10u128.pow(24));

        testing_env!(as_account(accounts(1)).build());
        c.pay(accounts(2), accounts(3), U128(5 * 10u128.pow(23)));
        let view = c.budget_of(accounts(2));
        assert_eq!(view.spent.0, 5 * 10u128.pow(23));
        assert_eq!(view.available.0, 15 * 10u128.pow(23));
    }

    #[test]
    #[should_panic(expected = "only the co-pilot agent can pay")]
    fn stranger_cannot_spend() {
        let mut c = setup();
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        testing_env!(as_account(accounts(3)).build());
        c.pay(accounts(2), accounts(4), U128(1));
    }

    #[test]
    #[should_panic(expected = "budget exceeded")]
    fn cannot_exceed_budget() {
        let mut c = setup();
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        c.pay(accounts(2), accounts(3), U128(2 * 10u128.pow(24)));
    }

    #[test]
    fn withdraw_revokes_unspent_budget() {
        let mut c = setup();
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        testing_env!(as_account(accounts(1)).build());
        c.pay(accounts(2), accounts(3), U128(4 * 10u128.pow(23)));
        testing_env!(as_account(accounts(2)).build());
        let _ = c.withdraw(U128(6 * 10u128.pow(23)));
        let view = c.budget_of(accounts(2));
        assert_eq!(view.available.0, 0);
        assert_eq!(view.spent.0, 4 * 10u128.pow(23));
    }

    #[test]
    fn additional_deposit_raises_limit() {
        let mut c = setup();
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        assert_eq!(c.budget_of(accounts(2)).available.0, 2 * 10u128.pow(24));
    }

    #[test]
    fn on_chain_per_payment_cap_is_enforced() {
        let mut c = setup();
        // Back to the default cap for this test.
        testing_env!(as_account(accounts(0)).build());
        c.set_max_payment(U128(DEFAULT_MAX_PAYMENT));
        testing_env!(as_account(accounts(2)).attached_deposit(NearToken::from_near(1)).build());
        c.deposit();
        assert_eq!(c.get_max_payment().0, DEFAULT_MAX_PAYMENT);
        testing_env!(as_account(accounts(1)).build());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            c.pay(accounts(2), accounts(3), U128(DEFAULT_MAX_PAYMENT + 1));
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
}
