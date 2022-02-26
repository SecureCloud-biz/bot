from abc import abstractmethod
from enum import Enum
from typing import Dict, List, Optional, Type

from discord.ext.commands import BadArgument, Context, Converter

from bot.exts.filtering._filter_context import FilterContext
from bot.exts.filtering._filters.filter import Filter
from bot.exts.filtering._settings import ActionSettings, ValidationSettings, create_settings
from bot.exts.filtering._utils import FieldRequiring, past_tense
from bot.log import get_logger

log = get_logger(__name__)


class ListType(Enum):
    """An enumeration of list types."""

    DENY = 0
    ALLOW = 1


class ListTypeConverter(Converter):
    """A Converter to get the appropriate list type."""

    aliases = (
        (ListType.DENY, {"deny", "blocklist", "blacklist", "denylist", "bl", "dl"}),
        (ListType.ALLOW, {"allow", "allowlist", "whitelist", "al", "wl"})
    )

    async def convert(self, ctx: Context, argument: str) -> ListType:
        """Get the appropriate list type."""
        for list_type, aliases in self.aliases:
            if argument in aliases or argument in map(past_tense, aliases):
                return list_type
        raise BadArgument(f"No matching list type found for {argument!r}.")


class FilterList(FieldRequiring):
    """Dispatches events to lists of _filters, and aggregates the responses into a single list of actions to take."""

    # Each subclass must define a name matching the filter_list name we're expecting to receive from the database.
    # Names must be unique across all filter lists.
    name = FieldRequiring.MUST_SET_UNIQUE

    def __init__(self, filter_type: Type[Filter]):
        self.filter_lists: dict[ListType, list[Filter]] = {}
        self.defaults = {}

        self.filter_type = filter_type

    def add_list(self, list_data: Dict) -> None:
        """Add a new type of list (such as a whitelist or a blacklist) this filter list."""
        actions, validations = create_settings(list_data["settings"])
        list_type = ListType(list_data["list_type"])
        self.defaults[list_type] = {"actions": actions, "validations": validations}

        filters = []
        for filter_data in list_data["filters"]:
            try:
                filters.append(self.filter_type(filter_data, actions))
            except TypeError as e:
                log.warning(e)
        self.filter_lists[list_type] = filters

    @abstractmethod
    async def actions_for(self, ctx: FilterContext) -> tuple[Optional[ActionSettings], Optional[str]]:
        """Dispatch the given event to the list's filters, and return actions to take and a message to relay to mods."""

    @staticmethod
    def filter_list_result(ctx: FilterContext, filters: List[Filter], defaults: ValidationSettings) -> list[Filter]:
        """
        Sift through the list of filters, and return only the ones which apply to the given context.

        The strategy is as follows:
        1. The default settings are evaluated on the given context. The default answer for whether the filter is
        relevant in the given context is whether there aren't any validation settings which returned False.
        2. For each filter, its overrides are considered:
            - If there are no overrides, then the filter is relevant if that is the default answer.
            - Otherwise it is relevant if there are no failed overrides, and any failing default is overridden by a
            successful override.

        If the filter is relevant in context, see if it actually triggers.
        """
        passed_by_default, failed_by_default = defaults.evaluate(ctx)
        default_answer = not bool(failed_by_default)

        relevant_filters = []
        for filter_ in filters:
            if not filter_.validations:
                if default_answer and filter_.triggered_on(ctx):
                    relevant_filters.append(filter_)
            else:
                passed, failed = filter_.validations.evaluate(ctx)
                if not failed and failed_by_default < passed:
                    if filter_.triggered_on(ctx):
                        relevant_filters.append(filter_)

        return relevant_filters