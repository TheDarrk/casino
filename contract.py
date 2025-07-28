from near_sdk_py import Contract, call, view, init, ONE_NEAR
from typing import Dict, List, Optional
from near_sdk_py.promises import Promise
import json

class TeamBettingContract(Contract):
    """
    A team-based betting contract where:
    - Two teams compete for points
    - Users bet NEAR tokens on teams (minimum 0.5N)
    - Points are awarded based on deposit time (early = more points)
    - Admin sets pot size, commission, and game duration
    - Winners share the pot proportionally based on points
    - Losers get partial refund after deducting pot + commission (based on NEAR bet amount)
    - Admin can pause/unpause, force refund games, and ban players
    """

    @init # TGAS: ~5 Tgas
    def initialize(self, admin_id: str):
        """Initialize the contract with admin"""
        self.storage["admin"] = admin_id
        self.storage["game_active"] = False
        self.storage["game_started"] = False
        self.storage["game_start_time"] = 0
        self.storage["pot_size"] = 0
        self.storage["commission_rate"] = 10  # 10% default
        self.storage["game_duration"] = 3600  # 1 hour default (in seconds)
        self.storage["paused"] = False
        self.storage["force_refund_mode"] = False
        self.storage["banned_players"] = {}  # Track banned players
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""
        self.storage["withdrawable"] = {}  # NEW: Track how much each user can withdraw
        self.storage["point_rates"] = [24, 23, 22, 21, 20, 19, 18, 17, 16, 15]  # Points per NEAR for each hour

    def assert_admin(self):
        """Ensure only admin can call this function"""
        admin = self.storage.get("admin")
        if self.predecessor_account_id != admin:
            raise Exception("Only admin can call this function")

    def assert_not_paused(self):
        """Ensure contract is not paused"""
        if self.storage.get("paused", False):
            raise Exception("Contract is paused")

    def assert_not_banned(self, user_id: str):
        """Ensure user is not banned"""
        banned_players = self.storage.get("banned_players", {})
        if user_id in banned_players and banned_players[user_id]:
            raise Exception("Player account is banned")

    # Ban/Unban Player Functions
    @call  # TGAS: ~15 Tgas
    def ban_player(self, player_id: str):
        """Admin bans a player account from participating in betting"""
        self.assert_admin()
        self.assert_not_paused()
        banned_players = self.storage.get("banned_players", {})
        banned_players[player_id] = True
        self.storage["banned_players"] = banned_players
        self.log_event("player_banned", {
            "admin": self.predecessor_account_id,
            "banned_player": player_id
        })

    @call  # TGAS: ~15 Tgas
    def unban_player(self, player_id: str):
        """Admin unbans a player account"""
        self.assert_admin()
        self.assert_not_paused()
        banned_players = self.storage.get("banned_players", {})
        if player_id in banned_players:
            banned_players[player_id] = False
        self.storage["banned_players"] = banned_players
        self.log_event("player_unbanned", {
            "admin": self.predecessor_account_id,
            "unbanned_player": player_id
        })

    @call  # TGAS: ~10 Tgas
    def pause_game(self):
        """Admin can pause the contract - no functions will work until unpaused"""
        self.assert_admin()
        self.storage["paused"] = True
        self.log_event("game_paused", {"admin": self.predecessor_account_id})

    @call  # TGAS: ~10 Tgas
    def unpause_game(self):
        """Admin can unpause the contract"""
        self.assert_admin()
        self.storage["paused"] = False
        self.log_event("game_unpaused", {"admin": self.predecessor_account_id})

    @call  # TGAS: ~20 Tgas
    def set_game_duration(self, duration_seconds: int):
        """Admin sets game duration in seconds"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change game duration during active game")
        if duration_seconds < 60:
            raise Exception("Game duration must be at least 60 seconds")
        self.storage["game_duration"] = duration_seconds
        self.log_event("game_duration_set", {"duration_seconds": duration_seconds})

    @call  # TGAS: ~20 Tgas
    def set_pot_size(self, pot_size: int):
        """Admin sets the winning pot size in NEAR tokens"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change pot size during active game")
        self.storage["pot_size"] = pot_size
        self.log_event("pot_size_set", {"pot_size": pot_size})

    @call  # TGAS: ~20 Tgas
    def set_commission_rate(self, rate: int):
        """Admin sets commission rate (percentage)"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Cannot change commission rate during active game")
        if rate < 0 or rate > 50:
            raise Exception("Commission rate must be between 0 and 50 percent")
        self.storage["commission_rate"] = rate
        self.log_event("commission_rate_set", {"rate": rate})

    @call  # TGAS: ~30 Tgas
    def start_game(self):
        """Admin starts the betting game"""
        self.assert_admin()
        self.assert_not_paused()
        if self.storage.get("game_active"):
            raise Exception("Game already active")
        pot_size = self.storage.get("pot_size", 0)
        if pot_size <= 0:
            raise Exception("Pot size must be set first")

        self.storage["game_active"] = True
        self.storage["game_started"] = True
        self.storage["game_start_time"] = self.block_timestamp
        self.storage["force_refund_mode"] = False
        self.storage["team_a_bets"] = {}
        self.storage["team_b_bets"] = {}
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.storage["winning_team"] = ""
        self.storage["withdrawable"] = {}  # Clear withdrawable on new game

        self.log_event("game_started", {
            "pot_size": pot_size,
            "start_time": self.block_timestamp,
            "duration": self.storage.get("game_duration", 3600)
        })

    @call  # TGAS: ~50 Tgas
    def bet_on_team(self, team: str):
        """User bets NEAR tokens on a team (A or B) with minimum 0.5N and banned check"""
        self.assert_not_paused()
        user_id = self.predecessor_account_id
        self.assert_not_banned(user_id)
        if not self.storage.get("game_active"):
            raise Exception("No active game")
        if self.storage.get("force_refund_mode", False):
            raise Exception("Game is in refund mode - betting disabled")
        if team not in ["A", "B"]:
            raise Exception("Team must be 'A' or 'B'")
        if self.attached_deposit == 0:
            raise Exception("Must attach NEAR tokens to bet")

        minimum_bet = ONE_NEAR // 2  # 0.5 NEAR in yoctoNEAR
        if self.attached_deposit < minimum_bet:
            raise Exception("Minimum betting amount is 0.5 NEAR")

        bet_amount = self.attached_deposit
        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)  # nanoseconds to hours

        if hours_elapsed >= len(self.storage.get("point_rates", [])):
            point_rate = 1
        else:
            point_rate = self.storage.get("point_rates", [])[int(hours_elapsed)]

        points_earned = (bet_amount // ONE_NEAR) * point_rate

        team_key = f"team_{team.lower()}_bets"
        team_bets = self.storage.get(team_key, {})

        if user_id in team_bets:
            existing_bet = team_bets[user_id]
            team_bets[user_id] = {
                "amount": existing_bet["amount"] + bet_amount,
                "points": existing_bet["points"] + points_earned
            }
        else:
            team_bets[user_id] = {
                "amount": bet_amount,
                "points": points_earned
            }

        self.storage[team_key] = team_bets

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

    @call  # TGAS: ~100 Tgas
    def force_end_game_refund(self):
        """Admin ends the game and refunds all players fully"""
        self.assert_admin()
        self.assert_not_paused()
        if not self.storage.get("game_active"):
            raise Exception("No active game")

        self.storage["game_active"] = False
        self.storage["force_refund_mode"] = True

        team_a_bets = self.storage.get("team_a_bets", {})
        team_b_bets = self.storage.get("team_b_bets", {})
        withdrawable = self.storage.get("withdrawable", {})
        total_refunded = 0

        for user_id, bet_info in team_a_bets.items():
            refund_amount = bet_info["amount"]
            total_refunded += refund_amount
            withdrawable[user_id] = refund_amount
            self.log_event("force_refund", {
                "user": user_id,
                "team": "A",
                "refund_amount": refund_amount,
                "original_bet": bet_info["amount"]
            })

        for user_id, bet_info in team_b_bets.items():
            refund_amount = bet_info["amount"]
            total_refunded += refund_amount
            withdrawable[user_id] = refund_amount
            self.log_event("force_refund", {
                "user": user_id,
                "team": "B",
                "refund_amount": refund_amount,
                "original_bet": bet_info["amount"]
            })

        self.storage["withdrawable"] = withdrawable

        self.log_event("game_force_ended", {
            "admin": self.predecessor_account_id,
            "total_refunded": total_refunded,
            "refund_mode": True
        })

    @call  # TGAS: ~80 Tgas
    def end_game(self):
        """Admin ends the game and triggers payouts"""
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

        self._distribute_payouts()

    def _distribute_payouts(self):  # TGAS: ~150 Tgas
        winning_team = self.storage.get("winning_team")
        pot_size = self.storage.get("pot_size", 0) * ONE_NEAR
        commission_rate = self.storage.get("commission_rate", 10)

        winning_team_key = f"team_{winning_team.lower()}_bets"
        losing_team_key = f"team_{'a' if winning_team == 'B' else 'b'}_bets"

        winning_bets = self.storage.get(winning_team_key, {})
        losing_bets = self.storage.get(losing_team_key, {})

        winning_total_amount = sum(bet["amount"] for bet in winning_bets.values())
        losing_total_amount = sum(bet["amount"] for bet in losing_bets.values())

        winning_total_points = sum(bet["points"] for bet in winning_bets.values())

        commission_amount = (pot_size * commission_rate) // 100
        total_to_pay = pot_size + commission_amount

        withdrawable = self.storage.get("withdrawable", {})

        # Distribute to winners
        for user_id, bet_info in winning_bets.items():
            user_payout = bet_info["amount"]
            pot_share = 0
            if winning_total_points > 0:
                pot_share = (bet_info["points"] * pot_size) // winning_total_points
                user_payout += pot_share

            withdrawable[user_id] = user_payout

            self.log_event("winner_payout", {
                "user": user_id,
                "original_bet": bet_info["amount"],
                "pot_share": pot_share,
                "total_payout": user_payout
            })

        # Distribute to losers
        if losing_total_amount >= total_to_pay:
            for user_id, bet_info in losing_bets.items():
                user_loss = (bet_info["amount"] * total_to_pay) // losing_total_amount
                user_refund = bet_info["amount"] - user_loss

                withdrawable[user_id] = user_refund

                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": user_loss,
                    "refund": user_refund,
                    "loss_calculation": "based_on_near_amount"
                })
        else:
            for user_id, bet_info in losing_bets.items():
                withdrawable[user_id] = 0
                self.log_event("loser_payout", {
                    "user": user_id,
                    "original_bet": bet_info["amount"],
                    "loss": bet_info["amount"],
                    "refund": 0,
                    "loss_calculation": "total_loss"
                })

        self.storage["withdrawable"] = withdrawable

        admin = self.storage.get("admin")
        self.log_event("commission_payout", {
            "admin": admin,
            "commission": commission_amount
        })

    @call  # TGAS: ~30 Tgas
    def withdraw(self):
        """Users can withdraw winnings or refunds after game ends"""
        self.assert_not_paused()
        user_id = self.predecessor_account_id

        if self.storage.get("game_active", False):
            raise Exception("Cannot withdraw during active game")

        withdrawable = self.storage.get("withdrawable", {})
        amount = withdrawable.get(user_id, 0)

        if amount == 0:
            raise Exception("No withdrawable balance available")

        # FIXED: Use Promise.create_batch instead of self.promise_transfer
        transfer_promise = Promise.create_batch(user_id).transfer(amount)

        # Clear the withdrawable balance
        withdrawable[user_id] = 0
        self.storage["withdrawable"] = withdrawable

        self.log_event("withdrawal", {
            "user": user_id,
            "amount": amount,
            "timestamp": self.block_timestamp
        })

        # Return the promise for proper transaction handling
        return transfer_promise


    # View functions
    @view  # TGAS: ~5 Tgas
    def get_game_status(self) -> Dict:
        return {
            "active": self.storage.get("game_active", False),
            "started": self.storage.get("game_started", False),
            "paused": self.storage.get("paused", False),
            "force_refund_mode": self.storage.get("force_refund_mode", False),
            "start_time": self.storage.get("game_start_time", 0),
            "pot_size": self.storage.get("pot_size", 0),
            "commission_rate": self.storage.get("commission_rate", 10),
            "game_duration": self.storage.get("game_duration", 3600),
            "team_a_points": self.storage.get("team_a_points", 0),
            "team_b_points": self.storage.get("team_b_points", 0),
            "winning_team": self.storage.get("winning_team", "")
        }

    @view  # TGAS: ~10 Tgas
    def get_team_bets(self, team: str) -> Dict:
        if team not in ["A", "B"]:
            return {}
        team_key = f"team_{team.lower()}_bets"
        return self.storage.get(team_key, {})

    @view  # TGAS: ~8 Tgas
    def get_user_bet(self, user_id: str, team: str) -> Dict:
        if team not in ["A", "B"]:
            return {}
        team_key = f"team_{team.lower()}_bets"
        team_bets = self.storage.get(team_key, {})
        return team_bets.get(user_id, {})

    @view  # TGAS: ~8 Tgas
    def calculate_current_points(self, amount_near: int) -> int:
        if not self.storage.get("game_active"):
            return 0
        time_elapsed = self.block_timestamp - self.storage.get("game_start_time", 0)
        hours_elapsed = time_elapsed // (60 * 60 * 1000000000)
        if hours_elapsed >= len(self.storage.get("point_rates", [])):
            point_rate = 1
        else:
            point_rate = self.storage.get("point_rates", [])[int(hours_elapsed)]
        return amount_near * point_rate

    @view  # TGAS: ~5 Tgas
    def get_admin_info(self) -> Dict:
        return {
            "admin": self.storage.get("admin", ""),
            "paused": self.storage.get("paused", False),
            "pot_size": self.storage.get("pot_size", 0),
            "commission_rate": self.storage.get("commission_rate", 10),
            "game_duration": self.storage.get("game_duration", 3600)
        }

    @view  # TGAS: ~8 Tgas
    def is_player_banned(self, player_id: str) -> bool:
        banned_players = self.storage.get("banned_players", {})
        return banned_players.get(player_id, False)

    @view  # TGAS: ~10 Tgas
    def get_banned_players(self) -> List[str]:
        banned_players = self.storage.get("banned_players", {})
        return [player_id for player_id, is_banned in banned_players.items() if is_banned]
