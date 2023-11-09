import os
import sys
import json
import time
import wget
import pickle
import argparse
import requests
from tqdm import tqdm
from datetime import datetime

class DotAReplays:
    def __init__(self, player_id):
        print(' . initiating DotAReplays instance for player %d' % player_id)
        fn = './dr-%d.pkl' % player_id
        if os.path.exists(fn):
            print('   . found existing save at %s' % fn)
            with open(fn, 'rb') as f:
                saved = pickle.load(f)
            # TODO: check if pkl contains necessary values
            self.data = saved
        else:
            print('   . no save file found, initializing new instance')
            self.data = {'player_id': player_id, 
                         'matches': [], 
                         'cache': {},
                         'downloaded': []}

    def get_matches(self):
        print(' . getting matches')
        time.sleep(0.8)
        get = requests.get('https://api.opendota.com/api/players/%d/matches' % self.data['player_id'])
        if get.status_code != 200:
            sys.exit('  . error in fetching matches')
        self.data['matches'] = json.loads(get.text)
        print('   . found %d matches' % len(self.data['matches']))
        

    def get_details(self, refresh=False, n=20):
        print(' . getting match details')
        if refresh:
            to_get = self.data['matches'][:n]
        else:
            to_get = [match for match in self.data['matches'] if match['match_id'] not in self.data['cache']]
        print('   . found %d matches not in cache' % len(to_get))

        start = datetime.now()
        parsed = []
        failed = []
        for match in tqdm(to_get):
            # only request parse if match was within last 2 weeks
            if (start - datetime.fromtimestamp(match['start_time'])).total_seconds() < 1209600:
                time.sleep(0.8)
                post = requests.post('https://api.opendota.com/api/request/%d' % match['match_id'])
                if post.status_code != 200:
                    parsed.append('    . failed to request parse for match %d, response %d' % (match['match_id'], post.status_code))

            time.sleep(0.8)
            get = requests.get('https://api.opendota.com/api/matches/%d' % match['match_id'])
            if get.status_code != 200:
                failed.append(match['match_id'])
                continue
            self.data['cache'][match['match_id']] = json.loads(get.text)
        
        if parsed:
            for msg in parsed:
                print(msg)
        if failed:
            print('   . could not get details for matches %s' % failed)

    def get_downloads(self):
        print(' . downloading match replays')
        dir = './replays-%d/' % self.data['player_id']
        if not os.path.exists(dir):
            os.mkdir(dir)
        # scan downloads
        existing = [file for file in os.listdir(dir) if file[-8:] == '.dem.bz2' and int(file.split('/')[-1].split('_')[0]) in self.data['cache']]
        print('   . found %d existing downloads' % len(existing))

        start = datetime.now()
        success = 0
        failed = {}
        for match in tqdm(self.data['matches']):
            if (start - datetime.fromtimestamp(match['start_time'])).total_seconds() < 1209600:
                if match['match_id'] not in self.data['cache']:
                    failed[match['match_id']] = '    . details for match %d not found'
                    continue
                try:
                    replay_url = self.data['cache'][match['match_id']]['replay_url']
                except KeyError:
                    failed[match['match_id']] = '    . match %d replay_url not found'
                    continue
                if replay_url.split('/')[-1] in existing: 
                    continue
                try:
                    wget.download(replay_url, out=dir)
                    success += 1
                except:
                    failed[match['match_id']] = '    . match %d download failed'
        
        print('   . downloaded %d matches, failed %d' % (success, len(failed)))
        if failed:
            for id in failed: 
                print(failed[id] % id)
    
    def export(self):
        print(' . exporting pickle')
        fn = './dr-%d.pkl' % self.data['player_id']
        with open(fn, 'wb') as f:
            pickle.dump(self.data, f, protocol=pickle.HIGHEST_PROTOCOL)
        print('   . saved to file %s' % fn)


def arg_parse():
    parser = argparse.ArgumentParser(description='DotA replay downloader.', formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('player_id', metavar='ID', type=int, help='DotA player ID')
    parser.add_argument('-n', '--recent-matches', metavar='N', type=int, default=20, help='fetch N most recent matches, use N=-1 for all')
    parser.add_argument('-r', '--refresh', action='store_true', help='overwrite cache')
    return vars(parser.parse_args())


if __name__ == "__main__":
    print('DotA Replay Downloader')
    args = arg_parse()

    # player_id = 117718885
    if 'profile' not in json.loads(requests.get('https://api.opendota.com/api/players/%d' % args['player_id']).text):
        sys.exit(' . ERROR: player %d not found' % args['player_id'])

    dr = DotAReplays(args['player_id'])
    dr.get_matches()
    dr.get_details(refresh=args['refresh'], n=args['recent_matches'])
    dr.get_downloads()
    dr.export()
