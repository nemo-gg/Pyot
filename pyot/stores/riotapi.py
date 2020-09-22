from json.decoder import JSONDecodeError
from typing import Mapping, Dict, Any, Tuple, List
from logging import getLogger
from datetime import datetime, timedelta
import asyncio
import aiohttp

from pyot.core import exceptions as exc
from pyot.pipeline.token import PipelineToken, RequestToken
from pyot.pipeline.objects import StoreObject
from pyot.pipeline.handler import ErrorHandler
from pyot.limiters.core import BaseLimiter
from pyot.utils import import_class

LOGGER = getLogger(__name__)


class RiotAPI(StoreObject):
    unique = True
    store_type = "SERVICE"

    def __init__(self, game: str, api_key: str, rate_limiter: Mapping[str, str] = None, error_handling: Mapping[int, Any] = None, log_level: int = 10):
        handler = ErrorHandler()
        self._game = game
        self._api_key = api_key
        self._endpoints = RiotAPIEndpoint(game)
        self._rate_limiter = self.create_rate_limiter(rate_limiter if rate_limiter else {})
        self._handler_map = handler.create_handler(error_handling)
        self._log_level = log_level

    async def get(self, token: PipelineToken, session: aiohttp.ClientSession) -> Dict:
        method = token.method
        server = token.server
        url = await self._endpoints.resolve(token)
        headers = {
            "X-Riot-Token": self._api_key,
            "Accept-Charset": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        request_token = RequestToken()
        while await request_token.run_or_raise():
            try:
                limit_token = await self._rate_limiter.get_limit_token(server, server+method)
                if await limit_token.run_or_wait():
                    self._rate_limiter.validate_bucket(server, server+method, limit_token)
                    continue
                response = await session.request(method="GET", url=url, headers=headers)
                LOGGER.log(self._log_level, f"[Trace: {self._game.upper()} > RiotAPI] GET: {self._log_template(token)}")
            except Exception as e:
                LOGGER.warning(f"[Trace: {self._game.upper()} > RiotAPI] WARNING: '{e.__class__.__name__}: {e}' was raised during the request and ignored")
                response = None

            await self._rate_limiter.stream(response, server, server+method, limit_token)
            code = response.status if response is not None else 408

            if response and response.status == 200:
                try:
                    try:
                        return await asyncio.wait_for(response.json(encoding="utf-8"), timeout=5)
                    except JSONDecodeError:
                        return await asyncio.wait_for(response.text(), timeout=5)
                except asyncio.TimeoutError:
                    code = 602

            try:
                how = self._handler_map[code]
            except KeyError:
                how = self._handler_map[800]

            await self._check_backoff(response, server, method, code, self._log_template(token))
            await request_token.stream(code, how, self._log_template(token))
    
    async def _check_backoff(self, response: Any, server: str, method: str, code: int, origin: str):
        if code == 429 and hasattr(response, "headers") and "X-Rate-Limit-Type" in response.headers and response.headers["X-Rate-Limit-Type"] != "service":
            seconds = response.headers["Retry-After"] if "Retry-After" in response.headers else 5
            LOGGER.warning(f"[Trace: {self._game.upper()} > RiotAPI] WARNING: The server responded with non service 429 Rate Limited, interrupts your task if this persists. "
                           f"Origin: {origin}, Inmediate backoff: {seconds} seconds.")
            type_ = response.headers["X-Rate-Limit-Type"]
            await self._rate_limiter.inmediate_backoff(int(seconds), type_, server, server+method)

    def create_rate_limiter(self, dic) -> BaseLimiter:
        config = {key.lower(): val for (key, val) in dic.items()}
        try:
            limiter = import_class(config.pop("backend"))
        except KeyError:
            limiter = import_class('pyot.limiters.MemoryLimiter')
        config["game"] = self._game
        config["api_key"] = self._api_key
        return limiter(**config)


class RiotAPIEndpoint:
    _riot_endpoints = {
        "account_v1_by_puuid": "/riot/account/v1/accounts/by-puuid/{puuid}",
        "account_v1_by_riot_id": "/riot/account/v1/accounts/by-riot-id/{name}/{tag}",
        "account_v1_active_shard": "/riot/account/v1/active-shards/by-game/{game}/by-puuid/{puuid}",
    }

    _endpoints = {
        "lol": {
            "champion_v3_rotation": "/lol/platform/v3/champion-rotations",
            "champion_mastery_v4_by_champion_id": "/lol/champion-mastery/v4/champion-masteries/by-summoner/{summoner_id}/by-champion/{champion_id}",
            "champion_mastery_v4_all_mastery": "/lol/champion-mastery/v4/champion-masteries/by-summoner/{summoner_id}",
            "clash_v1_players": "/lol/clash/v1/players/by-summoner/{summoner_id}",
            "clash_v1_teams": "/lol/clash/v1/teams/{id}",
            "clash_v1_tournaments_by_team_id": "/lol/clash/v1/tournaments/by-team/{team_id}",
            "clash_v1_toutnaments_by_tournament_id": "/lol/clash/v1/tournaments/{id}",
            "clash_v1_tournaments_all": "/lol/clash/v1/tournaments",
            "league_v4_summoner_entries": "/lol/league/v4/entries/by-summoner/{summoner_id}",
            "league_v4_challenger_league": "/lol/league/v4/challengerleagues/by-queue/{queue}",
            "league_v4_grandmaster_league": "/lol/league/v4/grandmasterleagues/by-queue/{queue}",
            "league_v4_master_league": "/lol/league/v4/masterleagues/by-queue/{queue}",
            "league_v4_entries_by_division": "/lol/league/v4/entries/{queue}/{tier}/{division}",
            "league_v4_league_by_league_id": "/lol/league/v4/leagues/{id}",
            "status_v3_shard_data": "/lol/status/v3/shard-data",
            "match_v4_match": "/lol/match/v4/matches/{id}",
            "match_v4_timeline": "/lol/match/v4/timelines/by-match/{id}",
            "match_v4_matchlist": "/lol/match/v4/matchlists/by-account/{account_id}",
            "spectator_v4_current_game": "/lol/spectator/v4/active-games/by-summoner/{summoner_id}",
            "spectator_v4_featured_games": "/lol/spectator/v4/featured-games",
            "summoner_v4_by_name": "/lol/summoner/v4/summoners/by-name/{name}",
            "summoner_v4_by_id": "/lol/summoner/v4/summoners/{id}",
            "summoner_v4_by_account_id": "/lol/summoner/v4/summoners/by-account/{account_id}",
            "summoner_v4_by_puuid": "/lol/summoner/v4/summoners/by-puuid/{puuid}",
            "third_party_code_v4_code": "/lol/platform/v4/third-party-code/by-summoner/{summoner_id}",
        },
        "tft": {
            "league_v1_summoner_entries": "/tft/league/v1/entries/by-summoner/{summoner_id}",
            "league_v1_challenger_league": "/tft/league/v1/challenger",
            "league_v1_grandmaster_league": "/tft/league/v1/grandmaster",
            "league_v1_master_league": "/tft/league/v1/master",
            "league_v1_entries_by_division": "/tft/league/v1/entries/{tier}/{division}",
            "league_v1_league_by_league_id": "/tft/league/v1/leagues/{id}",
            "match_v1_matchlist": "/tft/match/v1/matches/by-puuid/{puuid}/ids",
            "match_v1_match": "/tft/match/v1/matches/{id}",
            "summoner_v1_by_name": "/tft/summoner/v1/summoners/by-name/{name}",
            "summoner_v1_by_id": "/tft/summoner/v1/summoners/{id}",
            "summoner_v1_by_account_id": "/tft/summoner/v1/summoners/by-account/{account_id}",
            "summoner_v1_by_puuid": "/tft/summoner/v1/summoners/by-puuid/{puuid}",
        },
        "val": {
            "match_v1_match": "/val/match/v1/matches/{id}",
            "match_v1_matchlist": "/val/match/v1/matchlists/by-puuid/{puuid}",
            "match_v1_recent": "/val/match/v1/recent-matches/by-queue/{queue}",
            "content_v1_contents": "/val/content/v1/contents",
        }
    }
    
    _base_url = "https://{server}.api.riotgames.com"

    def __init__(self, game):
        try:
            self.endpoints = self._endpoints[game]
        except KeyError as e:
            raise NotImplementedError(f"RiotAPI does not support '{e}' model")
        self.endpoints.update(self._riot_endpoints)

    async def resolve(self, token: PipelineToken) -> str:
        try:
            base = self._base_url.format(**{"server": token.server})
            url = self.endpoints[token.method].format(**token.params)
            query = ""
            for a, b in token.queries.items():
                if isinstance(b, list):
                    for val in b:
                        query = query + "&" + str(a) + "=" + str(val)
                else:
                    query = query + "&" + str(a) + "=" + str(b)
            if len(query) > 1:
                query= "?" + query[1:]
            return base + url + query
        except KeyError:
            raise exc.NotFindable
