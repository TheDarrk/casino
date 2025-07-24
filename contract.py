
from near_sdk_py import Contract, call, view, init, ONE_NEAR
from typing import Dict, List, Optional
import json

class TeamBettingContract(Contract):
    """
    A team-based betting contract where:
    - Two teams compete for points
    - Users bet NEAR tokens on teams
    - Points are awarded based on deposit time (early = more points)
    - Admin sets pot size and commission
    - Winners share the pot proportionally based on points
    - Losers get partial refund after deducting pot + commission
    """

    @init
    def initialize(self, admin_id: str):
        """Initialize the contract with admin"""
        self.storage["admin"] = admin_id
        self.storage["game_active"] = False
        self.storage["game_started"] = False
        self.storage["game_start_time"] = 0
        self.storage["pot_size"] = 0
        self.storage["commission_rate"] = 10  # 10% default
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""
        self.storage["point_rates"] = [24, 23, 22, 21, 20, 19, 18, 17, 16, 15]  # Points per NEAR for each hour

    def assert_admin(self):
        """Ensure only admin can call this function"""
        admin = self.storage.get("admin")
        if self.predecessor_account_id != admin:
            raise Exception("Only admin can call this function")

    @call
    def set_pot_size(self, pot_size: int):
        """Admin sets the winning pot size in NEAR tokens"""
        self.assert_admin()
        if self.storage.get("game_active"):
            raise Exception("Cannot change pot size during active game")

        self.storage["pot_size"] = pot_size
        self.log_event("pot_size_set", {"pot_size": pot_size})

    @call
    def set_commission_rate(self, rate: int):
        """Admin sets commission rate (percentage)"""
        self.assert_admin()
        if rate < 0 or rate > 50:
            raise Exception("Commission rate must be between 0 and 50 percent")

        self.storage["commission_rate"] = rate
        self.log_event("commission_rate_set", {"rate": rate})

    @call
    def start_game(self):
        """Admin starts the betting game"""
        self.assert_admin()
        if self.storage.get("game_active"):
            raise Exception("Game already active")

        pot_size = self.storage.get("pot_size", 0)
        if pot_size <= 0:
            raise Exception("Pot size must be set first")

        self.storage["game_active"] = True
        self.storage["game_started"] = True
        self.storage["game_start_time"] = self.block_timestamp
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""

        self.log_event("game_started", {
            "pot_size": pot_size,
            "start_time": self.block_timestamp
        })

    @call
    def bet_on_team(self, team: str):
        """User bets NEAR tokens on a team (A or B)"""
        if not self.storage.get("game_active"):
            raise Exception("No active game")

        if team not in ["A", "B"]:
            raise Exception("Team must be 'A' or 'B'")

        if self.attached_deposit == 0:
            raise Exception("Must attach NEAR tokens to bet")

        user_id = self.predecessor_account_id
        bet_amount = self.attached_deposit

        # Calculate points based on time elapsed since game start
        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)  # Convert nanoseconds to hours

        # Get point rate (24 points for first hour, then decreasing)
        if hours_elapsed >= len(self.storage.get("point_rates", [])):
            point_rate = 1  # Minimum 1 point per NEAR
        else:
            point_rate = self.storage.get("point_rates", [])[int(hours_elapsed)]

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

    @call
    def end_game(self):
        """Admin ends the game and determines winner"""
        self.assert_admin()
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

        # Trigger payout distribution
        self._distribute_payouts()

    def _distribute_payouts(self):
        """Internal function to distribute payouts to winners and losers"""
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

        # Calculate total points for proportional distribution
        winning_total_points = sum(bet["points"] for bet in winning_bets.values())

        # Calculate commission
        commission_amount = (pot_size * commission_rate) // 100
        net_pot = pot_size - commission_amount

        # Distribute to winners
        for user_id, bet_info in winning_bets.items():
            # User gets their original bet back
            user_payout = bet_info["amount"]

            # Plus proportional share of the pot
            if winning_total_points > 0:
                pot_share = (bet_info["points"] * net_pot) // winning_total_points
                user_payout += pot_share

            # Transfer to user (in real implementation, this would be a promise)
            self.log_event("winner_payout", {
                "user": user_id,
                "original_bet": bet_info["amount"],
                "pot_share": pot_share if winning_total_points > 0 else 0,
                "total_payout": user_payout
            })

        # Calculate what losers need to pay and what they get back
        total_to_pay = pot_size + commission_amount

        if losing_total_amount >= total_to_pay:
            # Losers can cover the pot + commission
            for user_id, bet_info in losing_bets.items():
                # Calculate proportional loss
                user_loss = (bet_info["amount"] * total_to_pay) // losing_total_amount
                user_refund = bet_info["amount"] - user_loss

                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": user_loss,
                    "refund": user_refund
                })
        else:
            # Losers lose everything (rare case)
            for user_id, bet_info in losing_bets.items():
                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": bet_info["amount"],
                    "refund": 0
                })

        # Pay commission to admin
        admin = self.storage.get("admin")
        self.log_event("commission_payout", {
            "admin": admin,
            "commission": commission_amount
        })

    @view
    def get_game_status(self) -> Dict:
        """Get current game status"""
        return {
            "active": self.storage.get("game_active", False),
            "started": self.storage.get("game_started", False),
            "start_time": self.storage.get("game_start_time", 0),
            "pot_size": self.storage.get("pot_size", 0),
            "commission_rate": self.storage.get("commission_rate", 10),
            "team_a_points": self.storage.get("team_a_points", 0),
            "team_b_points": self.storage.get("team_b_points", 0),
            "winning_team": self.storage.get("winning_team", "")
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
    def calculate_current_points(self, amount_near: int) -> int:
        """Calculate points that would be earned for betting now"""
        if not self.storage.get("game_active"):
            return 0

        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)

        if hours_elapsed >= len(self.storage.get("point_rates", [])):
            point_rate = 1
        else:
            point_rate = self.storage.get("point_rates", [])[int(hours_elapsed)]

        return amount_near * point_rate
