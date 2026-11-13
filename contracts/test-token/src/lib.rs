//! VeriPay Test Dollar (tUSD) — NEP-141 fungible token for NEAR testnet demos.
//!
//! This is a TESTNET DEMO TOKEN, not a stablecoin:
//! - `mint` is open so demo wallets can self-serve test funds.
//! - Do not deploy this to mainnet or use it with real value.
//!
//! On top of standard NEP-141 this token implements a minimal allowance
//! ("approve / spender") extension. It exists so a wallet can grant the
//! VeriPay AI co-pilot a scoped, revocable budget: one wallet signature, then
//! the agent may spend within that budget without a popup per payment. The
//! allowance — not the agent's key custody — is the security boundary.

use near_contract_standards::fungible_token::core::FungibleTokenCore;
use near_contract_standards::fungible_token::metadata::{FungibleTokenMetadata, FT_METADATA_SPEC};
use near_contract_standards::fungible_token::resolver::FungibleTokenResolver;
use near_contract_standards::fungible_token::FungibleToken;
use near_contract_standards::storage_management::{
    StorageBalance, StorageBalanceBounds, StorageManagement,
};
use near_sdk::json_types::U128;
use near_sdk::store::LookupMap;
use near_sdk::{env, near, require, AccountId, NearToken, PanicOnDefault, PromiseOrValue};

#[near(contract_state)]
#[derive(PanicOnDefault)]
pub struct TestToken {
    token: FungibleToken,
    metadata: FungibleTokenMetadata,
}

#[near]
impl TestToken {
    #[init]
    pub fn new(name: String, symbol: String, decimals: u8) -> Self {
        let owner = near_sdk::env::predecessor_account_id();
        let mut this = Self {
            token: FungibleToken::new(b"t".to_vec()),
            metadata: FungibleTokenMetadata {
                spec: FT_METADATA_SPEC.to_string(),
                name,
                symbol,
                icon: None,
                reference: None,
                reference_hash: None,
                decimals,
            },
        };
        this.token.internal_register_account(&owner);
        this
    }

    /// TESTNET DEMO ONLY: open mint so any demo wallet can get tUSD.
    /// Repeat mints to the same account are fine (the faucet depends on it):
    /// registration happens only when the account is not registered yet —
    /// `internal_register_account` panics for existing accounts.
    pub fn mint(&mut self, account_id: AccountId, amount: U128) {
        self.ensure_registered(&account_id);
        self.token.internal_deposit(&account_id, amount.0);
    }

    /// Register `account_id` unless it already holds an account record.
    fn ensure_registered(&mut self, account_id: &AccountId) {
        if !self.token.accounts.contains_key(account_id) {
            self.token.internal_register_account(account_id);
        }
    }

    pub fn ft_metadata(&self) -> FungibleTokenMetadata {
        self.metadata.clone()
    }

    // ------------------------------------------------------------------
    // Allowance extension ("approve / spender")
    //
    // The allowance map is constructed locally with a fixed prefix (never a
    // state struct field) so upgrades of an already-deployed token need no
    // migration.
    // ------------------------------------------------------------------

    /// Caller approves `spender` (e.g. the VeriPay AI agent account) to move
    /// up to `amount` tUSD from the caller's balance. Set 0 to revoke.
    pub fn approve(&mut self, spender: AccountId, amount: U128) {
        let owner = env::predecessor_account_id();
        allowances().insert((owner, spender), amount.0);
    }

    /// Remaining allowance owner has granted spender.
    pub fn ft_allowance(&self, owner: AccountId, spender: AccountId) -> U128 {
        U128(*allowances().get(&(owner, spender)).unwrap_or(&0))
    }

    /// Spend part of a previously approved allowance. Only callable by the
    /// spender; funds move from `owner` to `receiver_id`. Allowance is
    /// deducted before the transfer — a failed transfer rolls everything back.
    #[payable]
    pub fn ft_transfer_from(
        &mut self,
        owner: AccountId,
        receiver_id: AccountId,
        amount: U128,
        memo: Option<String>,
    ) {
        require!(
            env::attached_deposit() == NearToken::from_yoctonear(1),
            "requires 1 yoctoNEAR"
        );
        let spender = env::predecessor_account_id();
        let mut map = allowances();
        let current = *map.get(&(owner.clone(), spender.clone())).unwrap_or(&0);
        require!(current >= amount.0, "allowance exceeded");
        map.insert((owner.clone(), spender), current - amount.0);
        // Fresh wallets are not registered in the token yet and a plain
        // internal transfer would panic on them; this demo token subsidises
        // the one-time registration so agent payments just work.
        self.ensure_registered(&receiver_id);
        self.token
            .internal_transfer(&owner, &receiver_id, amount.0, memo);
    }
}

/// Standalone allowance storage — no struct field, no migration on upgrade.
fn allowances() -> LookupMap<(AccountId, AccountId), u128> {
    LookupMap::new(b"allowances".to_vec())
}

#[near]
impl FungibleTokenCore for TestToken {
    #[payable]
    fn ft_transfer(&mut self, receiver_id: AccountId, amount: U128, memo: Option<String>) {
        self.token.ft_transfer(receiver_id, amount, memo)
    }

    #[payable]
    fn ft_transfer_call(
        &mut self,
        receiver_id: AccountId,
        amount: U128,
        memo: Option<String>,
        msg: String,
    ) -> PromiseOrValue<U128> {
        self.token.ft_transfer_call(receiver_id, amount, memo, msg)
    }

    fn ft_total_supply(&self) -> U128 {
        self.token.ft_total_supply()
    }

    fn ft_balance_of(&self, account_id: AccountId) -> U128 {
        self.token.ft_balance_of(account_id)
    }
}

#[near]
impl FungibleTokenResolver for TestToken {
    #[private]
    fn ft_resolve_transfer(
        &mut self,
        sender_id: AccountId,
        receiver_id: AccountId,
        amount: U128,
    ) -> U128 {
        self.token.ft_resolve_transfer(sender_id, receiver_id, amount)
    }
}

#[near]
impl StorageManagement for TestToken {
    #[payable]
    fn storage_deposit(
        &mut self,
        account_id: Option<AccountId>,
        registration_only: Option<bool>,
    ) -> StorageBalance {
        self.token.storage_deposit(account_id, registration_only)
    }

    #[payable]
    fn storage_withdraw(&mut self, amount: Option<NearToken>) -> StorageBalance {
        self.token.storage_withdraw(amount)
    }

    #[payable]
    fn storage_unregister(&mut self, force: Option<bool>) -> bool {
        self.token.internal_storage_unregister(force).is_some()
    }

    fn storage_balance_bounds(&self) -> StorageBalanceBounds {
        self.token.storage_balance_bounds()
    }

    fn storage_balance_of(&self, account_id: AccountId) -> Option<StorageBalance> {
        self.token.storage_balance_of(account_id)
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
            .account_balance(NearToken::from_near(50));
        b
    }

    fn setup() -> TestToken {
        testing_env!(as_account(accounts(0)).build());
        TestToken::new("VeriPay Test Dollar".to_string(), "tUSD".to_string(), 6)
    }

    #[test]
    fn approve_spend_and_deduct() {
        let mut c = setup();
        c.mint(accounts(1), U128(1_000));
        c.mint(accounts(3), U128(1)); // register receiver

        testing_env!(as_account(accounts(1)).build());
        c.approve(accounts(2), U128(500));
        assert_eq!(c.ft_allowance(accounts(1), accounts(2)).0, 500);

        let mut ctx = as_account(accounts(2));
        ctx.attached_deposit(NearToken::from_yoctonear(1));
        testing_env!(ctx.build());
        c.ft_transfer_from(accounts(1), accounts(3), U128(200), None);

        assert_eq!(c.ft_balance_of(accounts(1)).0, 800);
        assert_eq!(c.ft_balance_of(accounts(3)).0, 201);
        assert_eq!(c.ft_allowance(accounts(1), accounts(2)).0, 300);
    }

    #[test]
    #[should_panic(expected = "allowance exceeded")]
    fn cannot_exceed_allowance() {
        let mut c = setup();
        c.mint(accounts(1), U128(1_000));
        testing_env!(as_account(accounts(1)).build());
        c.approve(accounts(2), U128(100));

        let mut ctx = as_account(accounts(2));
        ctx.attached_deposit(NearToken::from_yoctonear(1));
        testing_env!(ctx.build());
        c.ft_transfer_from(accounts(1), accounts(3), U128(101), None);
    }

    #[test]
    #[should_panic(expected = "requires 1 yoctoNEAR")]
    fn requires_one_yocto() {
        let mut c = setup();
        c.mint(accounts(1), U128(1_000));
        testing_env!(as_account(accounts(1)).build());
        c.approve(accounts(2), U128(100));
        testing_env!(as_account(accounts(2)).build());
        c.ft_transfer_from(accounts(1), accounts(3), U128(50), None);
    }

    #[test]
    fn revoke_blocks_further_spending() {
        let mut c = setup();
        c.mint(accounts(1), U128(1_000));
        testing_env!(as_account(accounts(1)).build());
        c.approve(accounts(2), U128(100));
        c.approve(accounts(2), U128(0));
        assert_eq!(c.ft_allowance(accounts(1), accounts(2)).0, 0);

        let mut ctx = as_account(accounts(2));
        ctx.attached_deposit(NearToken::from_yoctonear(1));
        testing_env!(ctx.build());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            c.ft_transfer_from(accounts(1), accounts(3), U128(1), None);
        }));
        assert!(result.is_err(), "spend after revoke must fail");
    }

    #[test]
    fn repeat_mint_same_account_works() {
        // Regression: internal_register_account panics for existing accounts,
        // which used to break the faucet on its second use.
        let mut c = setup();
        c.mint(accounts(1), U128(100));
        c.mint(accounts(1), U128(50));
        assert_eq!(c.ft_balance_of(accounts(1)).0, 150);
        assert_eq!(c.ft_total_supply().0, 150);
    }

    #[test]
    fn transfer_from_registers_fresh_receiver() {
        let mut c = setup();
        c.mint(accounts(1), U128(1_000));
        testing_env!(as_account(accounts(1)).build());
        c.approve(accounts(2), U128(400));

        let mut ctx = as_account(accounts(2));
        ctx.attached_deposit(NearToken::from_yoctonear(1));
        testing_env!(ctx.build());
        // accounts(3) was never registered before this call.
        c.ft_transfer_from(accounts(1), accounts(3), U128(150), None);
        assert_eq!(c.ft_balance_of(accounts(3)).0, 150);
        assert_eq!(c.ft_allowance(accounts(1), accounts(2)).0, 250);
    }
}
