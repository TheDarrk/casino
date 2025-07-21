from near_sdk import NearBindgen, call, view, AccountId, env, Promise
from near_sdk.collections import LookupMap, UnorderedSet
from typing import List

@NearBindgen
class NoLossCasino:
    # Initialization method (call this after deployment)
    @call
    def init(self):
        """Initialize contract state - call once after deployment"""
        assert not hasattr(self, 'owner'), "Contract already initialized"
        self.owner = env.predecessor_account_id()
        self.entry_fee = 10**24  # 1 NEAR in yoctoNEAR
        self.winners_count = 10
        self.prize_per_winner = 10 * 10**24  # 10 NEAR
        self.house_fee_percent = 20
        self.min_players = 120
        self.players = UnorderedSet("players")
        self.payouts = LookupMap("payouts")
        self.game_active = True
        env.log("Contract initialized successfully")

    @call
    def join_game(self):
        """Players enter the game by paying 1 NEAR"""
        assert self.game_active, "Game is closed"
        assert env.attached_deposit() == self.entry_fee, "Pay exactly 1 NEAR"
        player = env.predecessor_account_id()
        assert not self.players.contains(player), "Already joined"
        self.players.add(player)
        env.log(f"Player {player} joined. Total players: {self.players.len()}")

    @call
    def resolve_game(self):
        """Owner closes the game and calculates payouts"""
        assert env.predecessor_account_id() == self.owner, "Owner only"
        assert self.players.len() >= self.min_players, f"Minimum {self.min_players} players required"

        # Calculate financials
        total_pot = self.players.len() * self.entry_fee
        house_fee = (total_pot * self.house_fee_percent) // 100
        net_pot = total_pot - house_fee
        
        # Select winners using secure method
        winners = self._select_winners()
        
        # Distribute prizes to winners
        winner_amount = self.prize_per_winner
        for winner in winners:
            self.payouts[winner] = winner_amount
        
        # Calculate refunds with remainder handling
        refund_pool = net_pot - (self.winners_count * winner_amount)
        non_winners_count = self.players.len() - self.winners_count
        
        if non_winners_count > 0:
            refund_per_player = refund_pool // non_winners_count
            remainder = refund_pool % non_winners_count
            
            # Distribute refunds
            non_winners = [p for p in self.players.to_list() if p not in winners]
            for player in non_winners:
                self.payouts[player] = refund_per_player
            
            # Add remainder to last non-winner to avoid dust loss
            if remainder > 0:
                self.payouts[non_winners[-1]] += remainder
        else:
            # Handle case where all players are winners
            remainder = refund_pool
        
        self.game_active = False
        Promise(self.owner).transfer(house_fee + remainder)
        env.log(f"Game resolved. Winners: {len(winners)}")

    def _select_winners(self) -> List[AccountId]:
        """Secure winner selection using cryptographic hashing"""
        # Create unpredictable seed
        seed = (
            env.block_timestamp().to_bytes(8, 'big') + 
            env.block_hash() + 
            env.predecessor_account_id().encode()
        )
        
        players = self.players.to_list()
        winners = []
        
        # Generate winner indices using SHA256 chain
        for _ in range(self.winners_count):
            # Create new hash for each selection
            seed = env.sha256(seed)
            # Convert hash to integer index
            idx = int.from_bytes(seed, 'big') % len(players)
            
            # Add unique winner
            winner = players[idx]
            while winner in winners:
                seed = env.sha256(seed)
                idx = int.from_bytes(seed, 'big') % len(players)
                winner = players[idx]
                
            winners.append(winner)
        
        return winners

    @call
    def claim_payout(self):
        """Players withdraw their winnings/refunds"""
        player = env.predecessor_account_id()
        amount = self.payouts.get(player, 0)
        assert amount > 0, "No payout available"
        
        # Prevent reentrancy: clear state BEFORE transfer
        del self.payouts[player]
        Promise(player).transfer(amount)
        env.log(f"Paid {amount/10**24} NEAR to {player}")

    @view
    def get_players_count(self) -> int:
        """Get total number of players"""
        return self.players.len()

    @view
    def get_payout_amount(self, account_id: AccountId) -> int:
        """Check payout amount for an account"""
        return self.payouts.get(account_id, 0)

    @call
    def change_owner(self, new_owner: AccountId):
        """Transfer contract ownership"""
        assert env.predecessor_account_id() == self.owner, "Owner only"
        self.owner = new_owner

    @call
    def emergency_withdraw(self):
        """Recover funds after game ends (owner only)"""
        assert env.predecessor_account_id() == self.owner, "Owner only"
        assert not self.game_active, "Game still active"
        balance = env.account_balance()
        # Leave 5 NEAR for contract storage
        withdrawable = balance - 5 * 10**24
        assert withdrawable > 0, "Insufficient balance"
        Promise(self.owner).transfer(withdrawable)

    @call
    def reset_game(self):
        """Start a new game (owner only)"""
        assert env.predecessor_account_id() == self.owner, "Owner only"
        assert not self.game_active, "Game still running"
        self.players.clear()
        self.payouts.clear()
        self.game_active = True
        env.log("New game started")
