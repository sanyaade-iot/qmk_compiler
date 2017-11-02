import logging
import qmk_redis
from glob import glob
from os import chdir
from os.path import exists
from qmk_commands import checkout_qmk, memoize
from rq.decorators import job
from subprocess import check_output, STDOUT
from time import strftime
import json

default_key_entry = {'x':-1, 'y':-1, 'w':1}


@memoize
def list_keyboards():
    """Extract the list of keyboards from qmk_firmware.
    """
    chdir('qmk_firmware')
    try:
        keyboards = check_output(('make', 'list-keyboards'), stderr=STDOUT, universal_newlines=True)
        keyboards = keyboards.strip()
        keyboards = keyboards.split('\n')[-1]
    finally:
        chdir('..')
    return keyboards.split()


@memoize
def find_all_layouts(keyboard):
    """Looks for layout macros associated with this keyboard.
    """
    layouts = {}
    rules_mk = parse_rules_mk('qmk_firmware/keyboards/%s/rules.mk' % keyboard)

    # Pull in all keymaps defined in the standard files
    current_path = 'qmk_firmware/keyboards/'
    for directory in keyboard.split('/'):
        current_path += directory + '/'
        if exists('%s/%s.h' % (current_path, directory)):
            layouts.update(find_layouts('%s/%s.h' % (current_path, directory)))

    if not layouts:
        # If we didn't find any layouts above we widen our search. This is error
        # prone which is why we want to encourage people to follow the standard above.
        logging.warning('%s: Falling back to searching for KEYMAP/LAYOUT macros.', keyboard)

        if 'DEFAULT_FOLDER' in rules_mk:
            layout_dir = 'qmk_firmware/keyboards/' + rules_mk['DEFAULT_FOLDER']
        else:
            layout_dir = 'qmk_firmware/keyboards/' + keyboard

        for file in glob(layout_dir + '/*.h'):
            if file.endswith('.h'):
                these_layouts = find_layouts(file)
                if these_layouts:
                    layouts.update(these_layouts)

    if 'LAYOUTS' in rules_mk:
        # Match these up against the supplied layouts
        supported_layouts = rules_mk['LAYOUTS'].strip().split()
        for layout_name in sorted(layouts):
            if not layout_name.startswith('LAYOUT_'):
                continue
            layout_name = layout_name[7:]
            if layout_name in supported_layouts:
                supported_layouts.remove(layout_name)

        if supported_layouts:
            logging.warning('*** %s: Missing layout pp macro for %s', keyboard, supported_layouts)

    return layouts


def parse_rules_mk(file, rules_mk=None):
    """Turn a rules.mk file into a dictionary.
    """
    if not rules_mk:
        rules_mk = {}

    if not exists(file):
        return {}

    for line in open(file).readlines():
        line = line.strip().split('#')[0]
        if not line:
            continue

        if '=' in line:
            if '+=' in line:
                key, value = line.split('+=')
                if key.strip() not in rules_mk:
                    rules_mk[key.strip()] = value.strip()
                else:
                    rules_mk[key.strip()] += ' ' + value.strip()
            elif '=' in line:
                key, value = line.split('=', 1)
                rules_mk[key.strip()] = value.strip()

    return rules_mk


def default_key(entry=None):
    """Increment x and return a copy of the default_key_entry.
    """
    default_key_entry['x'] += 1
    return default_key_entry.copy()


@memoize
def find_layouts(file):
    """Returns list of parsed layout macros found in the supplied file.
    """
    aliases = {}  # Populated with all `#define`s that aren't functions
    source_code=open(file).readlines()
    writing_keymap=False
    discovered_keymaps=[]
    parsed_keymaps={}
    current_keymap=[]
    for line in source_code:
        if not writing_keymap:
            if '#define' in line and '(' in line and ('LAYOUT' in line or 'KEYMAP' in line):
                writing_keymap=True
            elif '#define' in line:
                try:
                    _, pp_macro_name, pp_macro_text = line.strip().split(' ', 2)
                    aliases[pp_macro_name] = pp_macro_text
                except ValueError:
                    continue
        if writing_keymap:
            current_keymap.append(line.strip()+'\n')
            if ')' in line:
                writing_keymap=False
                discovered_keymaps.append(''.join(current_keymap))
                current_keymap=[]

    for keymap in discovered_keymaps:
        # Clean-up the keymap text, extract the macro name, and end up with a list
        # of key entries.
        keymap = keymap.replace('\\', '').replace(' ', '').replace('\t','').replace('#define', '')
        macro_name, keymap = keymap.split('(', 1)
        keymap = keymap.split(')', 1)[0]

        # Reject any macros that don't start with `KEYMAP` or `LAYOUT`
        if not (macro_name.startswith('KEYMAP') or macro_name.startswith('LAYOUT')):
            continue

        # Parse the keymap entries into naive x/y data
        parsed_keymap = []
        default_key_entry['y'] = -1
        for row in keymap.strip().split(',\n'):
            default_key_entry['x'] = -1
            default_key_entry['y'] += 1
            parsed_keymap.extend([default_key() for key in row.split(',')])
        parsed_keymaps[macro_name] = parsed_keymap

    to_remove = set()
    for alias, text in aliases.items():
        if text in parsed_keymaps:
            parsed_keymaps[alias] = parsed_keymaps[text]
            to_remove.add(text)
    for macro in to_remove:
        del(parsed_keymaps[macro])

    return parsed_keymaps


@memoize
def find_info_json(keyboard):
    """Finds all the info.json files associated with keyboard.
    """
    info_json_path = 'qmk_firmware/keyboards/%s%s/info.json'
    rules_mk_path = 'qmk_firmware/keyboards/%s/rules.mk' % keyboard
    files = []

    for path in ('/../../../..', '/../../..', '/../..', '/..', ''):
        if (exists(info_json_path % (keyboard, path))):
            files.append(info_json_path % (keyboard, path))

    if exists(rules_mk_path):
        rules = parse_rules_mk(rules_mk_path)
        if 'DEFAULT_FOLDER' in rules:
            if (exists(info_json_path % (rules['DEFAULT_FOLDER'], path))):
                files.append(info_json_path % (rules['DEFAULT_FOLDER'], path))

    return files


def merge_info_json(info_fd, keyboard_info):
    try:
        info_json = json.load(info_fd)
    except Exception as e:
        logging.error("%s is invalid JSON!", info_fd.name)
        logging.exception(e)
        return keyboard_info

    if not isinstance(info_json, dict):
        logging.error("%s is invalid! Should be a JSON dict object.", info_fd.name)
        return keyboard_info

    for key in ('keyboard_name', 'manufacturer', 'identifier', 'url', 'maintainer', 'processor', 'bootloader', 'width', 'height'):
        if key in info_json:
            keyboard_info[key] = info_json[key]

    if 'layouts' in info_json:
        for layout_name, layout in info_json['layouts'].items():
            # Only pull in layouts we have a macro for
            if layout_name in keyboard_info['layouts']:
                keyboard_info['layouts'][layout_name] = layout

    return keyboard_info


@job('default', connection=qmk_redis.redis)
def update_kb_redis():
    checkout_qmk()
    kb_list = []
    cached_json = {'last_updated': strftime('%Y-%m-%d %H:%M:%S %Z'), 'keyboards': {}}
    for keyboard in list_keyboards():
        keyboard_info = {
            'last_updated': strftime('%Y-%m-%d %H:%M:%S %Z'),
            'keyboard_name': keyboard,
            'keyboard_folder': keyboard,
            'layouts': {},
            'maintainer': 'qmk',
        }
        for layout_name, layout_json in find_all_layouts(keyboard).items():
            keyboard_info['layouts'][layout_name] = layout_json

        for info_json_filename in find_info_json(keyboard):
            # Iterate through all the possible info.json files to build the final keyboard JSON.
            with open(info_json_filename) as info_file:
                keyboard_info = merge_info_json(info_file, keyboard_info)

        # Write the keyboard to redis and add it to the master list.
        qmk_redis.set('qmk_api_kb_'+keyboard, keyboard_info)
        kb_list.append(keyboard)
        cached_json['keyboards'][keyboard] = keyboard_info

    qmk_redis.set('qmk_api_keyboards', kb_list)
    qmk_redis.set('qmk_api_kb_all', cached_json)
    qmk_redis.set('qmk_api_last_updated', strftime('%Y-%m-%d %H:%M:%S %Z'))

    return True


if __name__ == '__main__':
    update_kb_redis()