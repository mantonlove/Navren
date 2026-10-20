// A local, user-seeded emblem in Navren's mist-blue palette.
import { createAvatar } from '@dicebear/core';

const cache = new Map();
const palettes = [
  ['f2f7fc', 'd6e6f4', '204c77', '387fae', '8fc4e5'],
  ['f0f6fb', 'cddfed', '244b71', '4b88b6', 'a5c9e6'],
  ['eff7f8', 'cfe5e9', '24536b', '438eaa', '93cbd7'],
  ['f2f5fb', 'dce3f0', '354f7c', '638cba', 'aecae8'],
];

const flow = {
  meta: { title: 'Navren — Personal Flow', creator: 'Navren' },
  schema: { type: 'object', properties: {} },
  create({ prng }) {
    const [paper, mist, ink, blue, glass] = prng.pick(palettes).map(color => '#' + color);
    const crest = prng.integer(-800, 800) / 100;
    const fold = prng.integer(-600, 600) / 100;
    const body = `
      <defs>
        <linearGradient id="field" x1="30" y1="14" x2="127" y2="151" gradientUnits="userSpaceOnUse"><stop stop-color="${paper}"/><stop offset="1" stop-color="${mist}"/></linearGradient>
        <linearGradient id="ribbon" x1="39" y1="105" x2="112" y2="36" gradientUnits="userSpaceOnUse"><stop stop-color="${ink}"/><stop offset=".6" stop-color="${blue}"/><stop offset="1" stop-color="${glass}"/></linearGradient>
        <linearGradient id="fold" x1="47" y1="125" x2="130" y2="65" gradientUnits="userSpaceOnUse"><stop stop-color="${glass}"/><stop offset=".52" stop-color="${blue}"/><stop offset="1" stop-color="${ink}"/></linearGradient>
        <linearGradient id="edge" x1="44" y1="119" x2="125" y2="51" gradientUnits="userSpaceOnUse"><stop stop-color="${paper}" stop-opacity=".3"/><stop offset=".55" stop-color="#ffffff" stop-opacity=".85"/><stop offset="1" stop-color="${glass}" stop-opacity=".4"/></linearGradient>
      </defs>
      <rect width="160" height="160" fill="url(#field)"/>
      <ellipse cx="82" cy="131" rx="39" ry="5" fill="${ink}" opacity=".06"/>
      <path d="M40 105C26 85 30 58 51 ${40 + crest}C73 ${21 + crest} 107 27 120 48C134 71 114 91 94 104C77 115 58 122 41 120C59 111 65 94 83 81C100 69 106 58 95 50C79 39 60 51 54 68C49 81 49 94 40 105Z" fill="url(#ribbon)"/>
      <path d="M37 116C56 112 70 ${94 + fold} 89 ${83 + fold}C105 ${74 + fold} 118 63 123 47C138 77 121 111 95 126C75 138 51 136 37 116Z" fill="url(#fold)"/>
      <path d="M39 116C60 111 73 ${92 + fold} 90 ${83 + fold}C106 ${74 + fold} 117 63 122 50" fill="none" stroke="url(#edge)" stroke-width="1.4" stroke-linecap="round"/>
      <path d="M42 103C34 84 39 60 56 ${44 + crest}C72 ${30 + crest} 93 31 105 38" fill="none" stroke="#ffffff" stroke-opacity=".38" stroke-width="1.1" stroke-linecap="round"/>`;
    return { attributes: { viewBox: '0 0 160 160' }, body };
  },
};

export function render(seed) {
  const key = String(seed || 'navren-guest');
  if (!cache.has(key)) {
    if (cache.size >= 128) cache.clear();
    cache.set(key, createAvatar(flow, { seed: 'navren-personal-flow-v3:' + key, size: 160 }).toDataUri());
  }
  return cache.get(key);
}
