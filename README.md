# DotA Replay Downloader

A small script to query OpenDota for match information, request match replay parsing, and download replay files.

## Dependencies

 This script requires `wget` for file downloads, `pickle` for caching, and `tqdm` for progress visualization. These can be installed by running `pip3 install wget pickle tqdm`.

 ## Usage

Your `player_id` can be found by logging into [OpenDota](https://www.opendota.com/) and going to `My Profile`. The URL should be in the form of `https://www.opendota.com/players/[player_id]`. Update this value in the script to track player matches.

Multiple players can be tracked simultaneously as the script uses the `player_id` in the cache filename. 