from typing import Dict, Optional
from near_sdk_py import Contract, call, view, init, env, Panic
from near_sdk_py.collections import LookupMap, UnorderedSet
from near_sdk_py.constants import ONE_NEAR, ONE_TGAS
from near_sdk_py.promises import Promise

MIN_BET = int(0.5 * ONE_NEAR)          # 0.5 Ⓝ
MAX_GAS = 300 * ONE_TGAS               # 300 TGas hard protocol limit


class BetInfo:
    """Lightweight record stored per bettor inside a LookupMap."""
    def __init__(self, amount: int, points: int, team: str):
        self.amount       = amount        # yoctoⓃ staked
        self.points       = points        # points earned
        self.team         = team          # "A" | "B"
        self.withdrawable = 0             # yoctoⓃ claimable after settlement

    def to_dict(self):
        return {
            "amount":       self.amount,
            "points":       self.points,
            "team":         self.team,
            "withdrawable": self.withdrawable,
        }


class TeamBettingContract(Contract):
    # ───────────────────────── INITIALISATION ────────────────────────────
    @init
    def initialize(self, admin_id: str):
        self.storage["admin"]             = admin_id
        self.storage["paused"]            = False
        self.storage["game_active"]       = False
        self.storage["game_start_time"]   = 0                # nanoseconds
        self.storage["pot_size"]          = 0                # whole Ⓝ
        self.storage["commission_rate"]   = 0                # %
        self.storage["game_duration"]     = 3600             # seconds
        self.storage["force_refund_mode"] = False
        self.storage["winning_team"]      = ""

        # team aggregates (yoctoⓃ / pts)
        self.storage["team_a_total"]      = 0
        self.storage["team_b_total"]      = 0
        self.storage["team_a_points"]     = 0
        self.storage["team_b_points"]     = 0

        # stateful collections
        self.bets   = LookupMap("b")                        # acc → BetInfo
        self.banned = UnorderedSet("x")                     # banned accounts

        # linear decay table: 24 → 1 pt/Ⓝ
        self.point_rates = [max(1, 24 - i) for i in range(24)]

    # ──────────────────────────── GUARDS ─────────────────────────────────
    def _admin_only(self):
        if env.predecessor_account_id != self.storage["admin"]:
            raise Panic("Admin only")

    def _not_paused(self):
        if self.storage["paused"]:
            raise Panic("Contract paused")

    def _not_banned(self, account: str):
        if self.banned.contains(account):
            raise Panic("Account is banned")

    # ─────────────────────────── ADMIN OPS ───────────────────────────────
    @call
    def pause_game(self):
        self._admin_only()
        self.storage["paused"] = True

    @call
    def unpause_game(self):
        self._admin_only()
        self.storage["paused"] = False

    @call
    def set_pot_size(self, pot_size: int):
        """Set pot size (whole Ⓝ). Not allowed during active round."""
        self._admin_only()
        if self.storage["game_active"]:
            raise Panic("Active round")
        self.storage["pot_size"] = pot_size

    @call
    def set_commission_rate(self, commission_rate: int):
        """0 ≤ rate ≤ 50."""
        self._admin_only()
        if not 0 <= commission_rate <= 50:
            raise Panic("Rate 0–50 %")
        if self.storage["game_active"]:
            raise Panic("Active round")
        self.storage["commission_rate"] = commission_rate

    @call
    def set_game_duration(self, duration_seconds: int):
        """≥ 60 s; not during active round."""
        self._admin_only()
        if duration_seconds < 60:
            raise Panic("Min 60 s")
        if self.storage["game_active"]:
            raise Panic("Active round")
        self.storage["game_duration"] = duration_seconds

    # ─── Ban control
    @call
    def ban_player(self, account_id: str):
        self._admin_only()
        self.banned.add(account_id)

    @call
    def unban_player(self, account_id: str):
        self._admin_only()
        if self.banned.contains(account_id):
            self.banned.remove(account_id)

    # ──────────────────────── GAME LIFECYCLE ─────────────────────────────
    @call
    def start_game(self):
        self._admin_only()
        self._not_paused()
        if self.storage["game_active"]:
            raise Panic("Round already active")
        if self.storage["pot_size"] == 0:
            raise Panic("Pot not set")

        # reset round state
        self.storage["game_active"]       = True
        self.storage["force_refund_mode"] = False
        self.storage["game_start_time"]   = env.block_timestamp
        self.storage["winning_team"]      = ""

        self.storage["team_a_total"]  = 0
        self.storage["team_b_total"]  = 0
        self.storage["team_a_points"] = 0
        self.storage["team_b_points"] = 0
        self.bets.clear()

    @call
    def bet_on_team(self, team: str):
        self._not_paused()
        if not self.storage["game_active"]:
            raise Panic("No active round")
        if self.storage["force_refund_mode"]:
            raise Panic("Refund mode")
        if team not in ("A", "B"):
            raise Panic("Team must be 'A' or 'B'")

        bettor = env.predecessor_account_id
        self._not_banned(bettor)
        if self.bets.contains(bettor):
            raise Panic("Already bet")

        amount = env.attached_deposit
        if amount < MIN_BET:
            raise Panic("Min bet 0.5 Ⓝ")

        # point rate
        elapsed = (env.block_timestamp - self.storage["game_start_time"]) // 1_000_000_000
        idx     = min(elapsed // 3600, 23)
        rate    = self.point_rates[idx]
        points  = (amount // ONE_NEAR) * rate

        # record
        self.bets[bettor] = BetInfo(amount, points, team)
        if team == "A":
            self.storage["team_a_total"]  += amount
            self.storage["team_a_points"] += points
        else:
            self.storage["team_b_total"]  += amount
            self.storage["team_b_points"] += points

    # ─── Emergency full refund
    @call
    def force_end_game_refund(self):
        self._admin_only()
        self._not_paused()
        if not self.storage["game_active"]:
            raise Panic("No active round")

        self.storage["game_active"]       = False
        self.storage["force_refund_mode"] = True

        for acc, info in self.bets.items():
            info.withdrawable = info.amount
            self.bets[acc] = info  # update

    # ─── Normal end
    @call
    def end_game(self):
        self._admin_only()
        self._not_paused()
        if not self.storage["game_active"]:
            raise Panic("No active round")

        self.storage["game_active"] = False

        # tie → refund
        if self.storage["team_a_points"] == self.storage["team_b_points"]:
            self.force_end_game_refund()
            return

        win_side = "A" if self.storage["team_a_points"] > self.storage["team_b_points"] else "B"
        lose_side = "B" if win_side == "A" else "A"
        self.storage["winning_team"] = win_side

        win_pts  = self.storage["team_a_points"] if win_side == "A" else self.storage["team_b_points"]
        lose_dep = self.storage["team_b_total"]  if win_side == "A" else self.storage["team_a_total"]

        # convert pot & commission to yoctoⓃ
        pot_yocto   = self.storage["pot_size"] * ONE_NEAR
        commission  = pot_yocto * self.storage["commission_rate"] // 100
        penalty     = pot_yocto + commission

        # ── losers pay by NEAR stake
        for acc, info in self.bets.items():
            if info.team != lose_side:
                continue
            share = info.amount / lose_dep
            loss  = int(penalty * share)
            info.withdrawable = max(0, info.amount - loss)
            self.bets[acc] = info

        # ── winners share pot by points
        for acc, info in self.bets.items():
            if info.team != win_side:
                continue
            reward = int((info.points / win_pts) * pot_yocto)
            info.withdrawable = info.amount + reward
            self.bets[acc] = info

        # pay commission to admin
        if commission:
            Promise.create(self.storage["admin"]).transfer(commission)

    # ───────────────────────── WITHDRAWAL ────────────────────────────────
    @call
    def withdraw(self):
        caller = env.predecessor_account_id
        self._not_banned(caller)

        if not self.bets.contains(caller):
            raise Panic("Nothing to withdraw")
        info: BetInfo = self.bets.get(caller)
        amount = info.withdrawable
        if amount == 0:
            raise Panic("No balance")

        info.withdrawable = 0
        self.bets[caller] = info
        Promise.create(caller).transfer(amount)

    # ──────────────────────────── VIEWS ──────────────────────────────────
    @view
    def get_game_status(self) -> Dict:
        return {
            "admin":            self.storage["admin"],
            "paused":           self.storage["paused"],
            "game_active":      self.storage["game_active"],
            "force_refund":     self.storage["force_refund_mode"],
            "pot_size":         self.storage["pot_size"],
            "commission_rate":  self.storage["commission_rate"],
            "game_duration":    self.storage["game_duration"],
            "game_start_time":  self.storage["game_start_time"],
            "team_a_total":     self.storage["team_a_total"],
            "team_b_total":     self.storage["team_b_total"],
            "team_a_points":    self.storage["team_a_points"],
            "team_b_points":    self.storage["team_b_points"],
            "winning_team":     self.storage["winning_team"],
            "banned_count":     len(self.banned),
        }

    @view
    def preview_points(self) -> int:
        if not self.storage["game_active"]:
            return 0
        elapsed = (env.block_timestamp - self.storage["game_start_time"]) // 1_000_000_000
        idx     = min(elapsed // 3600, 23)
        return self.point_rates[idx]

    @view
    def get_user_bet(self, account_id: str) -> Dict:
        if not self.bets.contains(account_id):
            return {}
        return self.bets.get(account_id).to_dict()

    @view
    def is_banned(self, account_id: str) -> bool:
        return self.banned.contains(account_id)
