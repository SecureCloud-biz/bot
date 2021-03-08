import logging
import textwrap
from collections import ChainMap
from typing import Union

from discord import Color, Embed, Member, User
from discord.ext.commands import Cog, Context, group, has_any_role

from bot.api import ResponseCodeError
from bot.bot import Bot
from bot.constants import Channels, Guild, MODERATION_ROLES, STAFF_ROLES, Webhooks
from bot.converters import FetchedMember
from bot.exts.moderation.watchchannels._watchchannel import WatchChannel
from bot.pagination import LinePaginator
from bot.utils import time

log = logging.getLogger(__name__)


class TalentPool(WatchChannel, Cog, name="Talentpool"):
    """Relays messages of helper candidates to a watch channel to observe them."""

    def __init__(self, bot: Bot) -> None:
        super().__init__(
            bot,
            destination=Channels.talent_pool,
            webhook_id=Webhooks.talent_pool,
            api_endpoint='bot/nominations',
            api_default_params={'active': 'true', 'ordering': '-inserted_at'},
            logger=log,
            disable_header=True,
        )

    @group(name='talentpool', aliases=('tp', 'talent', 'nomination', 'n'), invoke_without_command=True)
    @has_any_role(*MODERATION_ROLES)
    async def nomination_group(self, ctx: Context) -> None:
        """Highlights the activity of helper nominees by relaying their messages to the talent pool channel."""
        await ctx.send_help(ctx.command)

    @nomination_group.command(name='watched', aliases=('all', 'list'), root_aliases=("nominees",))
    @has_any_role(*MODERATION_ROLES)
    async def watched_command(
        self, ctx: Context, oldest_first: bool = False, update_cache: bool = True
    ) -> None:
        """
        Shows the users that are currently being monitored in the talent pool.

        The optional kwarg `oldest_first` can be used to order the list by oldest nomination.

        The optional kwarg `update_cache` can be used to update the user
        cache using the API before listing the users.
        """
        await self.list_watched_users(ctx, oldest_first=oldest_first, update_cache=update_cache)

    @nomination_group.command(name='oldest')
    @has_any_role(*MODERATION_ROLES)
    async def oldest_command(self, ctx: Context, update_cache: bool = True) -> None:
        """
        Shows talent pool monitored users ordered by oldest nomination.

        The optional kwarg `update_cache` can be used to update the user
        cache using the API before listing the users.
        """
        await ctx.invoke(self.watched_command, oldest_first=True, update_cache=update_cache)

    @nomination_group.command(name='watch', aliases=('w', 'add', 'a'), root_aliases=("nominate",))
    @has_any_role(*STAFF_ROLES)
    async def watch_command(self, ctx: Context, user: FetchedMember, *, reason: str = '') -> None:
        """
        Relay messages sent by the given `user` to the `#talent-pool` channel.

        A `reason` for adding the user to the talent pool is optional.
        If given, it will be displayed in the header when relaying messages of this user to the channel.
        """
        if user.bot:
            await ctx.send(f":x: I'm sorry {ctx.author}, I'm afraid I can't do that. I only watch humans.")
            return

        if isinstance(user, Member) and any(role.id in STAFF_ROLES for role in user.roles):
            await ctx.send(":x: Nominating staff members, eh? Here's a cookie :cookie:")
            return

        if not await self.fetch_user_cache():
            await ctx.send(f":x: Failed to update the user cache; can't add {user}")
            return

        # Manual request with `raise_for_status` as False because we want the actual response
        session = self.bot.api_client.session
        url = self.bot.api_client._url_for(self.api_endpoint)
        kwargs = {
            'json': {
                'actor': ctx.author.id,
                'reason': reason,
                'user': user.id
            },
            'raise_for_status': False,
        }
        async with session.post(url, **kwargs) as resp:
            response_data = await resp.json()

            if resp.status == 400:
                if response_data.get('user', False):
                    await ctx.send(":x: The specified user can't be found in the database tables")
                elif response_data.get('actor', False):
                    await ctx.send(":x: You have already nominated this user")

                return
            else:
                resp.raise_for_status()

        self.watched_users[user.id] = response_data
        msg = f":white_check_mark: The nomination for {user} has been added to the talent pool"

        history = await self.bot.api_client.get(
            self.api_endpoint,
            params={
                "user__id": str(user.id),
                "active": "false",
                "ordering": "-inserted_at"
            }
        )

        if history:
            msg += f"\n\n({len(history)} previous nominations in total)"

        await ctx.send(msg)

    @nomination_group.command(name='history', aliases=('info', 'search'))
    @has_any_role(*MODERATION_ROLES)
    async def history_command(self, ctx: Context, user: FetchedMember) -> None:
        """Shows the specified user's nomination history."""
        result = await self.bot.api_client.get(
            self.api_endpoint,
            params={
                'user__id': str(user.id),
                'ordering': "-active,-inserted_at"
            }
        )
        if not result:
            await ctx.send(":warning: This user has never been nominated")
            return

        embed = Embed(
            title=f"Nominations for {user.display_name} `({user.id})`",
            color=Color.blue()
        )
        lines = [self._nomination_to_string(nomination) for nomination in result]
        await LinePaginator.paginate(
            lines,
            ctx=ctx,
            embed=embed,
            empty=True,
            max_lines=3,
            max_size=1000
        )

    @nomination_group.command(name='unwatch', aliases=('end', ), root_aliases=("unnominate",))
    @has_any_role(*MODERATION_ROLES)
    async def unwatch_command(self, ctx: Context, user: FetchedMember, *, reason: str) -> None:
        """
        Ends the active nomination of the specified user with the given reason.

        Providing a `reason` is required.
        """
        if await self.unwatch(user.id, reason):
            await ctx.send(f":white_check_mark: Messages sent by {user} will no longer be relayed")
        else:
            await ctx.send(":x: The specified user does not have an active nomination")

    @nomination_group.group(name='edit', aliases=('e',), invoke_without_command=True)
    @has_any_role(*MODERATION_ROLES)
    async def nomination_edit_group(self, ctx: Context) -> None:
        """Commands to edit nominations."""
        await ctx.send_help(ctx.command)

    @nomination_edit_group.command(name='reason')
    @has_any_role(*MODERATION_ROLES)
    async def edit_reason_command(self, ctx: Context, nomination_id: int, actor: FetchedMember, *, reason: str) -> None:
        """Edits the reason of a specific nominator in a specific active nomination."""
        try:
            nomination = await self.bot.api_client.get(f"{self.api_endpoint}/{nomination_id}")
        except ResponseCodeError as e:
            if e.response.status == 404:
                self.log.trace(f"Nomination API 404: Can't find a nomination with id {nomination_id}")
                await ctx.send(f":x: Can't find a nomination with id `{nomination_id}`")
                return
            else:
                raise

        if not nomination["active"]:
            await ctx.send(":x: Can't edit the reason of an inactive nomination.")
            return

        if not any(entry["actor"] == actor.id for entry in nomination["entries"]):
            await ctx.send(f":x: {actor} doesn't have an entry in this nomination.")
            return

        self.log.trace(f"Changing reason for nomination with id {nomination_id} of actor {actor} to {repr(reason)}")

        await self.bot.api_client.patch(
            f"{self.api_endpoint}/{nomination_id}",
            json={"actor": actor.id, "reason": reason}
        )
        await self.fetch_user_cache()  # Update cache
        await ctx.send(":white_check_mark: Successfully updated nomination reason.")

    @nomination_edit_group.command(name='end_reason')
    @has_any_role(*MODERATION_ROLES)
    async def edit_end_reason_command(self, ctx: Context, nomination_id: int, *, reason: str) -> None:
        """Edits the unnominate reason for the nomination with the given `id`."""
        try:
            nomination = await self.bot.api_client.get(f"{self.api_endpoint}/{nomination_id}")
        except ResponseCodeError as e:
            if e.response.status == 404:
                self.log.trace(f"Nomination API 404: Can't find a nomination with id {nomination_id}")
                await ctx.send(f":x: Can't find a nomination with id `{nomination_id}`")
                return
            else:
                raise

        if nomination["active"]:
            await ctx.send(":x: Can't edit the end reason of an active nomination.")
            return

        self.log.trace(f"Changing end reason for nomination with id {nomination_id} to {repr(reason)}")

        await self.bot.api_client.patch(
            f"{self.api_endpoint}/{nomination_id}",
            json={"end_reason": reason}
        )
        await self.fetch_user_cache()  # Update cache.
        await ctx.send(":white_check_mark: Updated the end reason of the nomination!")

    @Cog.listener()
    async def on_member_ban(self, guild: Guild, user: Union[User, Member]) -> None:
        """Remove `user` from the talent pool after they are banned."""
        await self.unwatch(user.id, "User was banned.")

    async def unwatch(self, user_id: int, reason: str) -> bool:
        """End the active nomination of a user with the given reason and return True on success."""
        active_nomination = await self.bot.api_client.get(
            self.api_endpoint,
            params=ChainMap(
                {"user__id": str(user_id)},
                self.api_default_params,
            )
        )

        if not active_nomination:
            log.debug(f"No active nominate exists for {user_id=}")
            return False

        log.info(f"Ending nomination: {user_id=} {reason=}")

        nomination = active_nomination[0]
        await self.bot.api_client.patch(
            f"{self.api_endpoint}/{nomination['id']}",
            json={'end_reason': reason, 'active': False}
        )
        self._remove_user(user_id)

        return True

    def _nomination_to_string(self, nomination_object: dict) -> str:
        """Creates a string representation of a nomination."""
        guild = self.bot.get_guild(Guild.id)
        entries = []
        for site_entry in nomination_object["entries"]:
            actor_id = site_entry["actor"]
            actor = guild.get_member(actor_id)

            reason = site_entry["reason"] or "*None*"
            created = time.format_infraction(site_entry["inserted_at"])
            entries.append(
                f"Actor: {actor.mention if actor else actor_id}\nCreated: {created}\nReason: {reason}"
            )

        entries_string = "\n\n".join(entries)

        active = nomination_object["active"]

        start_date = time.format_infraction(nomination_object["inserted_at"])
        if active:
            lines = textwrap.dedent(
                f"""
                ===============
                Status: **Active**
                Date: {start_date}
                Nomination ID: `{nomination_object["id"]}`

                {entries_string}
                ===============
                """
            )
        else:
            end_date = time.format_infraction(nomination_object["ended_at"])
            lines = textwrap.dedent(
                f"""
                ===============
                Status: Inactive
                Date: {start_date}
                Nomination ID: `{nomination_object["id"]}`

                {entries_string}

                End date: {end_date}
                Unwatch reason: {nomination_object["end_reason"]}
                ===============
                """
            )

        return lines.strip()


def setup(bot: Bot) -> None:
    """Load the TalentPool cog."""
    bot.add_cog(TalentPool(bot))
