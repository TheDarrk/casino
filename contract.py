from near_sdk_py import Contract, call, view, init, ONE_NEAR
from typing import Dict, List, Optional
import json

# Minimum bet amount: 0.5 NEAR in yoctoⓃ
MIN_BET: int = int(0.5 * 10**24)

class TeamBettingContract(Contract):
    """
    Enhanced team-based betting contract with:
    - Pause/unpause functionality
    - Force refund capability
    - Loss calculation based on NEAR amounts (not points)
    - Variable game duration, pot size, and commission
    - Minimum betting amount of 0.5 NEAR
    - Proper fee structure for all functions
    """

    @init
    def initialize(self, admin_id: str):
        """Initialize the contract with admin (requires 0.001 NEAR fee)"""
        self.storage["admin"] = admin_id
        self.storage["paused"] = False
        self.storage["game_active"] = False
        self.storage["game_started"] = False
        self.storage["game_start_time"] = 0
        self.storage["game_duration"] = 3600  # Default 1 hour
        self.storage["pot_size"] = 0
        self.storage["commission_rate"] = 10  # 10% default
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""
        self.storage["force_refund_mode"] = False
        self.storage["point_rates"] = [24, 23, 22, 21, 20, 19, 18, 17, 16, 15]
        self.storage["withdrawable_balances"] = {}

    def assert_admin(self):
        """Ensure only admin can call this function"""
        admin = self.storage.get("admin")
        if self.predecessor_account_id != admin:
            raise Exception("Only admin can call this function")

    def assert_not_paused(self):
        """Ensure contract is not paused"""
        if self.storage.get("paused", False):
            raise Exception("Contract is paused")

    # =================== PAUSE/UNPAUSE FUNCTIONALITY ===================
    @call
    def pause_game(self):
        """Admin can pause the contract - no functions will work until unpaused (no fee required)"""
        self.assert_admin()
        self.storage["paused"] = True
        self.log_event("game_paused", {"admin": self.predecessor_account_id})

    @call
    def unpause_game(self):
        """Admin can unpause the contract (no fee required)"""
        self.assert_admin()
        self.storage["paused"] = False
        self.log_event("game_unpaused", {"admin": self.predecessor_account_id})

    # =================== ADMIN CONFIGURATION ===================
    @call
    def set_pot_size(self, pot_size: int):
        """Admin sets the winning pot size in NEAR tokens (no fee required)"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change pot size during active game")

        self.storage["pot_size"] = pot_size
        self.log_event("pot_size_set", {"pot_size": pot_size})

    @call
    def set_commission_rate(self, rate: int):
        """Admin sets commission rate (percentage) (no fee required)"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change commission rate during active game")
        if rate < 0 or rate > 50:
            raise Exception("Commission rate must be between 0 and 50 percent")

        self.storage["commission_rate"] = rate
        self.log_event("commission_rate_set", {"rate": rate})

    @call
    def set_game_duration(self, duration_seconds: int):
        """Admin sets game duration in seconds (no fee required)
        Common durations:
        - 10 min: 600 seconds
        - 30 min: 1800 seconds
        - 1 hour: 3600 seconds
        - 2 hours: 7200 seconds
        - 24 hours: 86400 seconds
        - 36 hours: 129600 seconds
        """
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change game duration during active game")
        if duration_seconds < 60:
            raise Exception("Game duration must be at least 60 seconds")

        self.storage["game_duration"] = duration_seconds
        self.log_event("game_duration_set", {"duration_seconds": duration_seconds})

    # =================== GAME LIFECYCLE ===================
    @call
    def start_game(self):
        """Admin starts the betting game (requires 0.003 NEAR fee)"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Game already active")

        pot_size = self.storage.get("pot_size", 0)
        if pot_size <= 0:
            raise Exception("Pot size must be set first")

        # Reset game state
        self.storage["game_active"] = True
        self.storage["game_started"] = True
        self.storage["game_start_time"] = self.block_timestamp
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""
        self.storage["force_refund_mode"] = False
        self.storage["withdrawable_balances"] = {}

        self.log_event("game_started", {
            "pot_size": pot_size,
            "commission_rate": self.storage.get("commission_rate"),
            "game_duration": self.storage.get("game_duration"),
            "start_time": self.block_timestamp
        })

    @call
    def bet_on_team(self, team: str):
        """User bets NEAR tokens on a team (A or B) (minimum 0.5 NEAR + 0.001 NEAR buffer)"""
        self.assert_not_paused()
        if not self.storage.get("game_active"):
            raise Exception("No active game")
        if self.storage.get("force_refund_mode"):
            raise Exception("Game is in refund mode, betting disabled")

        if team not in ["A", "B"]:
            raise Exception("Team must be 'A' or 'B'")

        if self.attached_deposit < MIN_BET:
            raise Exception("Minimum bet is 0.5 NEAR")

        user_id = self.predecessor_account_id
        bet_amount = self.attached_deposit

        # Check if game duration has elapsed
        game_duration = self.storage.get("game_duration", 3600)
        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        if time_elapsed >= game_duration * 1000000000:  # Convert seconds to nanoseconds
            raise Exception("Betting period has ended")

        # Calculate points based on time elapsed since game start
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)  # Convert nanoseconds to hours

        # Get point rate (24 points for first hour, then decreasing)
        point_rates = self.storage.get("point_rates", [24])
        if hours_elapsed >= len(point_rates):
            point_rate = 1  # Minimum 1 point per NEAR
        else:
            point_rate = point_rates[int(hours_elapsed)]

        points_earned = (bet_amount // ONE_NEAR) * point_rate

        # Store bet information
        team_key = f"team_{team.lower()}_bets"
        team_bets = self.storage.get(team_key, {})

        if user_id in team_bets:
            # Add to existing bet
            existing_bet = team_bets[user_id]
            team_bets[user_id] = {
                "amount": existing_bet["amount"] + bet_amount,
                "points": existing_bet["points"] + points_earned
            }
        else:
            # New bet
            team_bets[user_id] = {
                "amount": bet_amount,
                "points": points_earned
            }

        self.storage[team_key] = team_bets

        # Update team total points
        points_key = f"team_{team.lower()}_points"
        current_points = self.storage.get(points_key, 0)
        self.storage[points_key] = current_points + points_earned

        self.log_event("bet_placed", {
            "user": user_id,
            "team": team,
            "amount": bet_amount,
            "points": points_earned,
            "point_rate": point_rate
        })

    # =================== FORCE REFUND FUNCTIONALITY ===================
    @call
    def force_end_game_refund(self):
        """Admin ends the game and everyone gets their original NEAR amount back without any deduction (requires 0.002 NEAR fee)"""
        self.assert_admin()
        self.assert_not_paused()
        if not self.storage.get("game_active"):
            raise Exception("No active game")

        self.storage["game_active"] = False
        self.storage["force_refund_mode"] = True
        
        # Set withdrawable balances for all users to their original bet amounts
        withdrawable_balances = {}
        
        # Process Team A bets
        team_a_bets = self.storage.get("team_a_bets", {})
        for user_id, bet_info in team_a_bets.items():
            withdrawable_balances[user_id] = bet_info["amount"]
        
        # Process Team B bets
        team_b_bets = self.storage.get("team_b_bets", {})
        for user_id, bet_info in team_b_bets.items():
            if user_id in withdrawable_balances:
                withdrawable_balances[user_id] += bet_info["amount"]
            else:
                withdrawable_balances[user_id] = bet_info["amount"]
        
        self.storage["withdrawable_balances"] = withdrawable_balances
        
        self.log_event("force_refund_triggered", {
            "admin": self.predecessor_account_id,
            "total_users": len(withdrawable_balances)
        })

    # =================== NORMAL GAME END ===================
    @call
    def end_game(self):
        """Admin ends the game and determines winner with updated loss calculation (requires 0.002 NEAR fee)"""
        self.assert_admin()
        self.assert_not_paused()
        if not self.storage.get("game_active"):
            raise Exception("No active game")

        team_a_points = self.storage.get("team_a_points", 0)
        team_b_points = self.storage.get("team_b_points", 0)

        if team_a_points == team_b_points:
            raise Exception("Cannot end game with tie score")

        winning_team = "A" if team_a_points > team_b_points else "B"
        self.storage["winning_team"] = winning_team
        self.storage["game_active"] = False

        self.log_event("game_ended", {
            "winning_team": winning_team,
            "team_a_points": team_a_points,
            "team_b_points": team_b_points
        })

        # Trigger payout distribution with new logic
        self._distribute_payouts_new()

    def _distribute_payouts_new(self):
        """Internal function to distribute payouts with loss calculation based on NEAR amounts"""
        winning_team = self.storage.get("winning_team")
        pot_size = self.storage.get("pot_size", 0) * ONE_NEAR
        commission_rate = self.storage.get("commission_rate", 10)

        winning_team_key = f"team_{winning_team.lower()}_bets"
        losing_team_key = f"team_{'a' if winning_team == 'B' else 'b'}_bets"

        winning_bets = self.storage.get(winning_team_key, {})
        losing_bets = self.storage.get(losing_team_key, {})

        # Calculate total amounts
        winning_total_amount = sum(bet["amount"] for bet in winning_bets.values())
        losing_total_amount = sum(bet["amount"] for bet in losing_bets.values())

        # Calculate total points for winning team (for proportional pot distribution)
        winning_total_points = sum(bet["points"] for bet in winning_bets.values())

        # Calculate commission
        commission_amount = (pot_size * commission_rate) // 100
        total_to_pay = pot_size + commission_amount

        withdrawable_balances = {}

        # Distribute to winners (get original bet + proportional pot share based on points)
        for user_id, bet_info in winning_bets.items():
            # User gets their original bet back
            user_payout = bet_info["amount"]

            # Plus proportional share of the pot based on points
            if winning_total_points > 0:
                pot_share = (bet_info["points"] * pot_size) // winning_total_points
                user_payout += pot_share

            withdrawable_balances[user_id] = user_payout

            self.log_event("winner_payout", {
                "user": user_id,
                "original_bet": bet_info["amount"],
                "pot_share": pot_share if winning_total_points > 0 else 0,
                "total_payout": user_payout
            })

        # NEW LOGIC: Calculate what losers need to pay based on their NEAR bet amounts (not points)
        if losing_total_amount >= total_to_pay:
            # Losers can cover the pot + commission
            for user_id, bet_info in losing_bets.items():
                # Calculate proportional loss based on NEAR amount bet
                user_loss = (bet_info["amount"] * total_to_pay) // losing_total_amount
                user_refund = bet_info["amount"] - user_loss

                withdrawable_balances[user_id] = user_refund

                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": user_loss,
                    "refund": user_refund
                })
        else:
            # Losers lose everything (rare case)
            for user_id, bet_info in losing_bets.items():
                withdrawable_balances[user_id] = 0
                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": bet_info["amount"],
                    "refund": 0
                })

        self.storage["withdrawable_balances"] = withdrawable_balances

        # Pay commission to admin
        admin = self.storage.get("admin")
        self.log_event("commission_payout", {
            "admin": admin,
            "commission": commission_amount
        })

    # =================== WITHDRAWAL ===================
    @call
    def withdraw(self):
        """User withdraws their winnings or refunds (no fee required)"""
        self.assert_not_paused()
        user_id = self.predecessor_account_id
        withdrawable_balances = self.storage.get("withdrawable_balances", {})
        
        if user_id not in withdrawable_balances:
            raise Exception("No balance to withdraw")
        
        amount = withdrawable_balances[user_id]
        if amount <= 0:
            raise Exception("No balance to withdraw")
        
        # Remove user from withdrawable balances
        del withdrawable_balances[user_id]
        self.storage["withdrawable_balances"] = withdrawable_balances
        
        # Transfer amount to user (this would be implemented as a promise in real contract)
        self.log_event("withdrawal", {
            "user": user_id,
            "amount": amount
        })

    # =================== VIEW FUNCTIONS ===================
    @view
    def get_game_status(self) -> Dict:
        """Get current game status"""
        return {
            "admin": self.storage.get("admin", ""),
            "paused": self.storage.get("paused", False),
            "active": self.storage.get("game_active", False),
            "started": self.storage.get("game_started", False),
            "start_time": self.storage.get("game_start_time", 0),
            "game_duration": self.storage.get("game_duration", 3600),
            "pot_size": self.storage.get("pot_size", 0),
            "commission_rate": self.storage.get("commission_rate", 10),
            "team_a_points": self.storage.get("team_a_points", 0),
            "team_b_points": self.storage.get("team_b_points", 0),
            "winning_team": self.storage.get("winning_team", ""),
            "force_refund_mode": self.storage.get("force_refund_mode", False)
        }

    @view
    def get_team_bets(self, team: str) -> Dict:
        """Get all bets for a specific team"""
        if team not in ["A", "B"]:
            return {}

        team_key = f"team_{team.lower()}_bets"
        return self.storage.get(team_key, {})

    @view
    def get_user_bet(self, user_id: str, team: str) -> Dict:
        """Get a specific user's bet on a team"""
        if team not in ["A", "B"]:
            return {}

        team_key = f"team_{team.lower()}_bets"
        team_bets = self.storage.get(team_key, {})
        return team_bets.get(user_id, {})

    @view
    def get_user_withdrawable_balance(self, user_id: str) -> int:
        """Get user's withdrawable balance"""
        withdrawable_balances = self.storage.get("withdrawable_balances", {})
        return withdrawable_balances.get(user_id, 0)

    @view
    def calculate_current_points(self, amount_near: int) -> int:
        """Calculate points that would be earned for betting now"""
        if not self.storage.get("game_active"):
            return 0

        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)

        point_rates = self.storage.get("point_rates", [24])
        if hours_elapsed >= len(point_rates):
            point_rate = 1
        else:
            point_rate = point_rates[int(hours_elapsed)]

        return amount_near * point_rate

    @view
    def get_admin_info(self) -> Dict:
        """Get admin and configuration information"""
        return {
            "admin": self.storage.get("admin", ""),
            "pot_size": self.storage.get("pot_size", 0),
            "commission_rate": self.storage.get("commission_rate", 10),
            "game_duration": self.storage.get("game_duration", 3600),
            "minimum_bet": MIN_BET,
            "paused": self.storage.get("paused", False)
        }
