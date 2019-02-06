"""
Async tasks to run in the background

TODO: Make this less spaghetti
"""

import asyncio
import logging
from functools import wraps
from aredis import RedisError
from datetime import datetime, timedelta

from modules.redisClient import redisConn
from modules import embedGenerators, structure, apis

logger = logging.getLogger(__name__)

ONE_MINUTE = 60  # Just makes things a little more readable
API_CHECK_INTERVAL = structure.config["apiCheckInterval"]
LAUNCH_SOON_DELTA = timedelta(minutes=structure.config["launchSoonDelta"])


def notificationTask(loopInterval):
    """
    Runs the original function every loopInterval Minutes
    The wrapped function will be passed the Discord client, subbedChannelIDs,
    notificationTaskStore, and nextLaunchJSON objects
    The wrapped function must return a list of channels to remove, as well as
    launchingSoonNotifSent and latestLaunchInfoEmbedDict vars to be saved to Redis
    """

    def wrapper(func):
        @wraps(func)
        async def task(client, *args):
            await client.wait_until_ready()
            logger.info(f"{func.__name__} started")
            while not client.is_closed():
                try:
                    subbedChannelIDs = await redisConn.smembers("subscribedChannels")
                    subbedChannelIDs = (int(cid) for cid in subbedChannelIDs)

                    notificationTaskStore = await redisConn.getNotificationTaskStore()

                    nextLaunchJSON = await apis.spacexApi.getNextLaunchJSON()

                    if nextLaunchJSON != -1:

                        # Call function with variables we know are needed
                        channelsToRemove, launchingSoonNotifSent, latestLaunchInfoEmbedDict = await func(
                            client, subbedChannelIDs, notificationTaskStore, nextLaunchJSON
                        )

                        # Save any changed data to redis
                        # Remove channels that we can't access anymore
                        for channelID in channelsToRemove:
                            logger.info(f"{channelID} is not a valid channel ID, removing")
                            await redisConn.srem(
                                "subscribedChannels", str(channelID).encode("UTF-8")
                            )

                        await redisConn.setNotificationTaskStore(
                            launchingSoonNotifSent, latestLaunchInfoEmbedDict
                        )
                    
                    else:
                        logger.info("nextLaunchJSON returned -1, skipping this cycle")

                except RedisError as e:
                    logger.error(f"Redis operation failed: {type(e).__name__}: {e}")
                finally:
                    await asyncio.sleep(ONE_MINUTE * loopInterval)

        return task

    return wrapper


@notificationTask(1)
async def launchingSoonNotifTask(
    client, subbedChannelIDs, notificationTaskStore, nextLaunchJSON
):
    channelsToRemove = []
    launchingSoonNotifSent = notificationTaskStore["launchingSoonNotifSent"]

    launchTimestamp = await structure.convertToInt(nextLaunchJSON["launch_date_unix"])
    # If launchTimestamp is an int carry on, else it is TBA
    if launchTimestamp:

        # Get timestamp for the time LAUNCH_SOON_DELTA from now
        currentTime = datetime.utcnow()
        timePlusDelta = (currentTime + LAUNCH_SOON_DELTA).timestamp()

        # If the launch time is within the next LAUNCH_SOON_DELTA
        # and if the launchTimestamp is not in the past
        if (
            timePlusDelta >= launchTimestamp
            and launchTimestamp >= currentTime.timestamp()
        ):
            if launchingSoonNotifSent == "False":

                logger.info(
                    f"Launch happening within {LAUNCH_SOON_DELTA}, sending notification"
                )
                launchingSoonNotifSent = "True"
                launchingSoonEmbed = await embedGenerators.genLaunchingSoonEmbed(
                    nextLaunchJSON
                )

                for channelID in subbedChannelIDs:
                    channel = client.get_channel(channelID)
                    if channel == None:
                        channelsToRemove.append(channelID)
                    else:
                        await client.safeSend(channel, launchingSoonEmbed)

                        try:
                            guildSettings = await redisConn.getGuildSettings(
                                channel.guild.id
                            )
                        except RedisError:
                            # If a redis error occurs here, it is better to send out
                            # the remaining notifications than to exit this block
                            pass
                        else:
                            # If there are settings saved for the guild
                            if guildSettings != 0:
                                # Ping the roles/users (mentions) requested
                                await client.safeSend(
                                    channel, guildSettings["rolesToMention"]
                                )

            else:
                logger.info(
                    f"Launch happening within {LAUNCH_SOON_DELTA}, launchingSoonNotifSent is {launchingSoonNotifSent}"
                )

    return (
        channelsToRemove,
        launchingSoonNotifSent,
        notificationTaskStore["latestLaunchInfoEmbedDict"],
    )


@notificationTask(API_CHECK_INTERVAL)
async def launchChangedNotifTask(
    client, subbedChannelIDs, notificationTaskStore, nextLaunchJSON
):
    channelsToRemove = []
    launchingSoonNotifSent = notificationTaskStore["launchingSoonNotifSent"]
    latestLaunchInfoEmbedDict = notificationTaskStore["latestLaunchInfoEmbedDict"]

    launchInfoEmbed, launchInfoEmbedSmall = await embedGenerators.genLaunchInfoEmbeds(
        nextLaunchJSON
    )
    launchInfoEmbedDict = launchInfoEmbed.to_dict()  # Only calculate this once

    if latestLaunchInfoEmbedDict != launchInfoEmbedDict:
        logger.info("Launch info changed, sending notifications")

        launchingSoonNotifSent = "False"
        latestLaunchInfoEmbedDict = launchInfoEmbedDict

        # New launch found, send all "subscribed" channels the embed
        for channelID in subbedChannelIDs:
            channel = client.get_channel(channelID)
            if channel == None:
                channelsToRemove.append(channelID)
            else:
                await client.safeSendLaunchInfo(
                    channel, launchInfoEmbed, launchInfoEmbedSmall, sendErr=False
                )

    return channelsToRemove, launchingSoonNotifSent, latestLaunchInfoEmbedDict
