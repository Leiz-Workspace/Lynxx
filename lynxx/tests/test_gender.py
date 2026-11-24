# coding=UTF-8
"""
Lynxx: gender filter tier 0 + safety blacklist tests (offline). The blacklist is its own option (-safety_blacklist,
off by default), independent of the gender filter (product decision 2026-11-18)
docs/plans/gender-filter-model.md: AC-G01..AC-G05, AC-S01..AC-S03
Run: python -m unittest lynxx.tests.test_gender
"""
#########################################
#
#

import json
import os
import pathlib
import tempfile
import unittest
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import safety  # noqa: E402
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.gender import GenderConf, GenderLabel, decide, infer_from_tags, rules_for_module  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402

H, M_, L_ = GenderConf.HIGH, GenderConf.MEDIUM, GenderConf.LOW
F, MA, B, N, U = GenderLabel.ONLY_FEMALE, GenderLabel.ONLY_MALE, GenderLabel.BOTH, GenderLabel.NONE, GenderLabel.UNKNOWN

# (module, tags, expected label, expected confidence, required flags)
CASES = [
    # danbooru (completeness .95)
    ('db', '1girl 1boy hetero', B, H, set()),
    ('db', '1girl solo', F, H, set()),
    ('db', '2girls yuri', F, H, set()),
    ('db', 'multiple_girls', F, H, set()),
    ('db', '1boy male_focus', MA, H, set()),
    ('db', 'multiple_boys yaoi', MA, H, set()),
    ('db', 'yuri', F, M_, set()),
    ('db', '1girl penis', B, H, set()),
    ('db', '1girl futanari solo', F, H, {'futa'}),
    ('db', 'futanari futa_with_male 1boy', B, H, {'futa'}),
    ('db', 'trap 1boy', MA, H, {'trap'}),
    ('db', 'otoko_no_ko', MA, H, {'trap'}),
    ('db', 'reverse_trap', F, H, {'trapFemale'}),
    ('db', '1girl 1other', F, M_, {'other'}),
    ('db', '1other', N, M_, {'other'}),
    ('db', 'no_humans scenery', N, H, set()),
    ('db', 'scenery sky cloud', U, None, set()),
    ('db', 'furry_female solo', F, H, {'anthro'}),
    ('db', '1girl disembodied_penis', B, H, set()),
    ('db', 'photo_(medium) 1girl', F, H, {'real'}),
    # female body parts mirror the male ones: with male evidence the post is 'both', never only_male
    ('db', '1boy pussy', B, H, set()),
    ('db', '1boy large_breasts', B, H, set()),
    ('db', 'breasts', F, H, set()),
    ('db', '1boy male_with_breasts breasts', MA, H, {'futaMale'}),  # the breasts belong to the man
    ('db', 'cuntboy 1boy pussy', MA, H, {'futaMale'}),
    ('db', '1girl futanari penis pussy', F, H, {'futa'}),
    # rule34 (completeness .85: count tags alone only reach medium)
    ('rx', '1girls female_only', F, H, set()),
    ('rx', '1girls solo', F, M_, set()),
    ('rx', 'female', F, M_, set()),
    ('rx', '1boy male_only', MA, H, set()),
    ('rx', 'straight', B, H, set()),
    ('rx', 'hetero', B, H, set()),
    ('rx', 'female male', B, H, set()),
    ('rx', 'female_only 1boy', MA, L_, set()),
    ('rx', 'futa_only', F, H, {'futa'}),
    ('rx', 'futanari 1futa solo', F, M_, {'futa'}),
    ('rx', 'cuntboy', MA, M_, {'futaMale'}),
    ('rx', 'femboy 1boy', MA, M_, {'trap'}),
    ('rx', '1girls male_pov', B, H, {'pov'}),
    ('rx', 'female penis futanari', F, M_, {'futa'}),
    ('rx', 'lesbian', F, M_, set()),
    ('rx', 'ambiguous_gender', N, M_, {'other'}),
    # owner report 2026-11-20 ('chỉ nam' + amputee showed women): danbooru style count tags and female body parts count
    ('rx', 'amputee 1boy 1girl breasts', B, H, set()),
    ('rx', 'amputee penis pussy breasts', B, H, set()),
    ('rx', '1girl', F, M_, set()),
    ('rx', 'female_focus 1boy', B, H, set()),
    ('rx', 'male_focus', MA, M_, set()),
    ('rx', '1boy vagina', B, H, set()),
    ('rx', '1boy female_pov', B, H, {'pov'}),
    ('rx', 'pussy breasts', F, M_, set()),  # like 'penis' alone -> only_male medium
    ('rx', 'cuntboy 1boy pussy', MA, M_, {'futaMale'}),
    ('rx', 'male_with_breasts male big_breasts', MA, M_, {'futaMale'}),
    ('rx', 'andromorph male pussy', MA, M_, {'futaMale'}),
    ('rx', 'gynomorph penis breasts', F, M_, {'futa'}),  # her penis is not a man (was 'both' before gynomorph was a futa flag)
    ('rx', 'femboy 1boy breasts', B, H, {'trap'}),  # conservative: not only_male
    ('rx', 'male_only big_breasts', F, L_, set()),  # evidence wins over the marker with confidence low, like 'female_only 1boy'
    # bbooru (aggregator: the rule34 table plus danbooru's count tags; posts measured on 2026-11-19)
    ('bb', '1girls female solo', F, M_, set()),
    ('bb', 'female_only 1girls', F, H, set()),
    ('bb', '1girl penis', B, H, set()),
    ('bb', '1girl solo', F, M_, set()),
    ('bb', 'furry_female', F, M_, {'anthro'}),
    ('bb', 'furry_female furry_male', B, H, {'anthro'}),
    ('bb', '1boy male_focus', MA, M_, set()),
    ('bb', '4girls futanari multiple_girls penis', F, M_, {'futa'}),
    ('bb', '1other', N, M_, {'other'}),
    ('bb', 'futa_with_male 1boy', B, H, {'futa'}),
    ('bb', 'amputee penis pussy breasts', B, H, set()),
    ('bb', 'female_focus 1boy', B, H, set()),
    # e621 (completeness .92, pair tags, solo marker)
    ('en', 'male solo anthro', MA, H, {'anthro'}),
    ('en', 'female solo', F, H, set()),
    ('en', 'male/female duo', B, H, set()),
    ('en', 'male/male', MA, H, set()),
    ('en', 'female/female', F, H, set()),
    ('en', 'gynomorph/male', B, H, {'futa'}),
    ('en', 'intersex solo', F, H, {'futa'}),
    ('en', 'andromorph', MA, H, {'futaMale'}),
    ('en', 'ambiguous_gender solo', N, M_, {'other'}),
    ('en', 'female male_pov', B, H, {'pov'}),
    ('en', 'male female_pov', B, H, {'pov'}),
    ('en', 'male solo faceless_female', B, H, set()),
    ('en', 'gynomorph/male breasts penis', B, H, {'futa'}),  # e621 body part tags stay neutral (intersex characters are common)
    # generic / unmeasured sites
    ('xb', '1girl solo', F, M_, set()),
    ('xb', 'amputee penis pussy breasts', B, H, set()),
    ('rs', 'female_focus 1boy', B, H, set()),
    ('rp', 'Some_Character Series_Name', U, None, set()),
    ('rp', 'yuri', F, M_, set()),
]


class GenderRulesTests(TestCase):
    def test_table(self) -> None:
        for module, tags, label, conf, flags in CASES:
            with self.subTest(module=module, tags=tags):
                res = infer_from_tags(tags.split(), rules_for_module(module))
                self.assertEqual(label, res.label)
                self.assertEqual(conf, res.conf)
                self.assertTrue(flags.issubset(res.flags), f'flags {sorted(res.flags)} lack {sorted(flags)}')

    def test_bbooru_has_its_own_table(self) -> None:
        bb, rx = rules_for_module('bb'), rules_for_module('rx')
        self.assertEqual('bb', bb.module)
        self.assertEqual(rx.completeness, bb.completeness)
        for name in ('female', 'male'):  # every rule34 count tag is kept
            for conf in (H, M_):
                self.assertLessEqual(getattr(rx, name)[conf], getattr(bb, name)[conf])
        # danbooru-style post: rule34's table misses 'furry_female' ('1girl' is in both since 2026-11-20)
        self.assertEqual(MA, infer_from_tags(['furry_female', 'penis'], rx).label)
        self.assertEqual(B, infer_from_tags(['furry_female', 'penis'], bb).label)
        for name in ('female_bodypart', 'male_bodypart'):
            self.assertLessEqual(getattr(rx, name), getattr(bb, name))

    def test_female_and_male_evidence_are_symmetric(self) -> None:
        # every table that reads male body parts reads the female ones too, and the gelbooru-like tables know both count styles
        for module in ('db', 'rx', 'bb', 'en', 'xb', 'rp'):
            with self.subTest(module=module):
                rules = rules_for_module(module)
                self.assertTrue(rules.male_bodypart)
                self.assertIn('female_pov', rules.female_bodypart)
                self.assertIn('faceless_female', rules.female_bodypart)
        for module in ('rx', 'bb', 'xb'):
            with self.subTest(module=module):
                rules = rules_for_module(module)
                self.assertTrue({'1girl', '1girls', 'female_focus'} <= rules.female[H])
                self.assertTrue({'1boy', 'male_focus'} <= rules.male[H])

    def test_female_body_part_evidence(self) -> None:
        rx = rules_for_module('rx')
        res = infer_from_tags(['amputee', 'penis', 'pussy', 'breasts'], rx)
        self.assertEqual(['penis', 'breasts', 'pussy'], list(res.evidence))
        self.assertFalse(decide(res, {'only_male'}, M_, False)[0])
        # a body part next to a count tag adds no evidence: the model's verdict rests on the weakest evidence tag
        self.assertEqual(('1girl',), infer_from_tags(['1girl', 'breasts'], rules_for_module('db')).evidence)

    def test_evidence_and_case(self) -> None:
        res = infer_from_tags(['1Girl', '1BOY', 'hetero'], rules_for_module('db'))
        self.assertEqual((B, H), (res.label, res.conf))
        self.assertIn('1girl', res.evidence)
        self.assertIn('1boy', res.evidence)

    def test_decide_threshold_and_strict(self) -> None:
        db = rules_for_module('db')
        futa = infer_from_tags(['1girl', 'futanari', 'solo'], db)
        self.assertEqual((True, F), decide(futa, {'only_female'}, M_, False))
        self.assertEqual((False, U), decide(futa, {'only_female'}, M_, True))  # AC-G03
        rx_medium = infer_from_tags(['1girls', 'solo'], rules_for_module('rx'))
        self.assertTrue(decide(rx_medium, {'only_female'}, M_, False)[0])
        self.assertEqual((False, U), decide(rx_medium, {'only_female'}, H, False))
        self.assertEqual((True, U), decide(rx_medium, {'unknown'}, H, False))


class SafetyTests(TestCase):
    def test_every_listed_tag_is_blocked(self) -> None:
        for tag in sorted(safety.BLOCKED_TAGS | safety.BLOCKED_TOKENS):
            with self.subTest(tag=tag):
                self.assertTrue(safety.is_blocked([tag]))
                self.assertTrue(safety.is_blocked([tag.upper()]))

    def test_token_matching(self) -> None:
        for tag in ('younger_female', 'Young', 'young_male', 'onee-shota', 'loli_(style)', 'cub/adult', 'very_young', 'teen_titans'):
            with self.subTest(tag=tag):
                self.assertTrue(safety.is_blocked(['1girl', tag]))
        for tag in ('cat_ears', 'adult', 'mature_female', 'childhood_friend', 'youkai', 'kidney', 'cube', 'scenery'):
            with self.subTest(tag=tag):
                self.assertFalse(safety.is_blocked([tag]))

    def test_extensions_can_only_add(self) -> None:
        self.assertFalse(safety.is_blocked(['some_site_specific_tag']))
        safety.register_extra_tags(['Some_Site_Specific_Tag'])
        self.assertTrue(safety.is_blocked(['some_site_specific_tag']))
        public = [n for n in dir(safety) if not n.startswith('_')]
        self.assertFalse([n for n in public if any(w in n.lower() for w in ('remove', 'unregister', 'disable', 'clear', 'allow'))])


POSTS = [
    (1, '1girl solo'), (2, '1boy male_focus'), (3, '1girl 1boy hetero'), (4, 'no_humans scenery'),
    (5, 'younger_female 1girl'), (6, 'aged_down 1girl solo'), (7, 'landscape'),
]


def _db_posts_json() -> bytes:
    return json.dumps([{
        'id': i, 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': tags, 'file_ext': 'jpg', 'md5': f'{i:032d}',
        'file_url': f'https://cdn.donmai.us/original/{i}.jpg', 'large_file_url': f'https://cdn.donmai.us/sample/{i}.jpg',
        'preview_file_url': f'https://cdn.donmai.us/180x180/{i}.jpg', 'rating': 'g', 'score': 1,
    } for i, tags in POSTS]).encode()


class GenderPipelineTests(TestCase):
    def _downloader_with_items(self):
        dwn = _make_downloader(ProcModule.DB)
        items = dwn._get_all_post_tags(_db_posts_json())
        dwn.items_raw_per_task[:] = items
        for it in items:
            info = dwn._extract_item_info(it)
            dwn.item_info_dict_per_task[info.id] = info
        return dwn

    def test_filters_only_female(self) -> None:  # AC-G04, AC-S01
        dwn = self._downloader_with_items()
        dwn.gender_select = frozenset({'only_female'})
        dwn.safety_blacklist = True
        parents: set[str] = {'5'}
        dwn._filter_items_safety(parents)
        self.assertEqual(2, dwn.safety_blocked_count)
        self.assertNotIn('5', parents)
        dwn._filter_items_by_gender(parents)
        kept = [dwn._extract_id(h) for h in dwn.items_raw_per_task]
        self.assertEqual(['1'], kept)
        self.assertEqual({'2', '3', '4', '5', '6', '7'}, dwn.filtered_out_ids_cache)
        self.assertEqual(4, dwn.gender_filtered_count)
        self.assertEqual({'only_female': 1, 'only_male': 1, 'both': 1, 'none': 1, 'unknown': 1}, dwn.gender_stats)

    def test_safety_blacklist_is_its_own_option(self) -> None:  # AC-S01
        # the gender filter alone blocks nothing: posts 5 and 6 (minor coded tags) are judged by gender like any other
        dwn = self._downloader_with_items()
        dwn.gender_select = frozenset({'only_female'})
        dwn._filter_items_safety(set())
        self.assertEqual((0, 7), (dwn.safety_blocked_count, len(dwn.items_raw_per_task)))
        dwn._filter_items_by_gender(set())
        self.assertEqual({'1', '5', '6'}, {dwn._extract_id(h) for h in dwn.items_raw_per_task})
        # the blacklist alone, without any gender filter, blocks them
        dwn = self._downloader_with_items()
        dwn.safety_blacklist = True
        dwn._filter_items_safety(set())
        dwn._filter_items_by_gender(set())
        self.assertEqual(2, dwn.safety_blocked_count)
        self.assertEqual({'1', '2', '3', '4', '7'}, {dwn._extract_id(h) for h in dwn.items_raw_per_task})

    def test_no_filter_no_change(self) -> None:  # AC-G05: without -gender and -safety_blacklist behavior is unchanged
        dwn = self._downloader_with_items()
        before = list(dwn.items_raw_per_task)
        dwn._filter_items_safety(set())
        dwn._filter_items_by_gender(set())
        self.assertEqual(before, dwn.items_raw_per_task)
        self.assertEqual(0, dwn.safety_blocked_count)

    def test_cli_args_and_conflicts(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            me = current_process()
            me.killed = False
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-gender', 'only_female,both', '-gender_conf', 'high', '-gender_strict', 'x']), False)
            self.assertEqual(frozenset({'only_female', 'both'}), dwn.gender_select)
            self.assertEqual((H, True), (dwn.gender_min_conf, dwn.gender_strict))
            self.assertFalse(dwn.safety_blacklist)  # off by default, the gender filter does not turn it on
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-safety_blacklist', 'x']), False)
            self.assertTrue(dwn.safety_blacklist)
            self.assertEqual(frozenset(), dwn.gender_select)
        with self.assertRaises(SystemExit):
            prepare_arglist(['-module', 'db', '-gender', 'girls_only', 'x'])

    def test_model_verdicts_from_predicted_tags(self) -> None:
        from lynxx.gender_model import classify
        v = classify({'1girl': 0.97, 'solo': 0.93, 'long_hair': 0.8})
        self.assertEqual(('only_female', H), (str(v.result.label), v.result.conf))
        self.assertEqual('model', v.result.source)
        self.assertEqual(M_, classify({'1girl': 0.7, 'solo': 0.9}).result.conf)  # weakest evidence 0.70 -> medium
        self.assertEqual(M_, classify({'1girl': 0.97, 'solo': 0.93}, video=True).result.conf)  # one frame of a video
        self.assertEqual('both', str(classify({'1girl': 0.9, '1boy': 0.9, 'hetero': 0.8}).result.label))
        self.assertEqual('none', str(classify({'no_humans': 0.95, 'scenery': 0.9}).result.label))
        self.assertEqual('loli', classify({'1girl': 0.9, 'solo': 0.9, 'loli': 0.2}).safety_tag)  # gate at 0.15
        self.assertEqual('', classify({'1girl': 0.9, 'solo': 0.9, 'loli': 0.1}).safety_tag)
        photo = classify({'1girl': 0.95, 'solo': 0.95, 'photo_(medium)': 0.9, 'realistic': 0.9})
        self.assertEqual(('unknown', True), (str(photo.result.label), photo.real))

    def test_model_combines_with_tags(self) -> None:
        from lynxx.gender import GenderLabel as GL
        from lynxx.gender import GenderResult as GR
        from lynxx.gender_model import combine, needs_model
        tag_high = GR(GL.ONLY_MALE, H)
        tag_low = GR(GL.ONLY_FEMALE, L_)
        unknown = GR(GL.UNKNOWN, None)
        model_f = GR(GL.ONLY_FEMALE, H, source='model')
        self.assertFalse(needs_model(tag_high, M_))
        self.assertTrue(needs_model(tag_low, M_) and needs_model(unknown, M_))
        self.assertFalse(needs_model(GR(GL.UNKNOWN, None, frozenset({'real'})), M_))  # photos of real people: no model
        self.assertIs(tag_high, combine(tag_high, model_f, M_))  # confident tags win, the model is not used
        agree = combine(tag_low, model_f, M_)
        self.assertEqual((GL.ONLY_FEMALE, H, 'tags+model'), (agree.label, agree.conf, agree.source))
        self.assertEqual(GL.UNKNOWN, combine(GR(GL.ONLY_MALE, L_), model_f, M_).label)  # disagreement: unknown
        self.assertEqual((GL.ONLY_FEMALE, 'model'), (combine(unknown, model_f, M_).label, combine(unknown, model_f, M_).source))
        self.assertEqual(GL.UNKNOWN, combine(GR(GL.UNKNOWN, None, frozenset({'trap'})), model_f, M_).label)  # never trap -> female

    def test_model_in_the_download_filter(self) -> None:
        from lynxx.gender import GenderLabel as GL
        from lynxx.gender import GenderResult as GR
        from lynxx.gender_model import ModelVerdict
        for safety_on in (True, False):  # the model's safety gate blocks only with -safety_blacklist
            with self.subTest(safety_blacklist=safety_on), _make_downloader(ProcModule.DB) as dwn:
                me = current_process()
                me.killed = False
                dwn.reset_root_thread(me)
                dwn.no_conflict_delay = True
                dwn._parse_args(prepare_arglist(['-module', 'db', '-gender', 'only_female', *(['-safety_blacklist'] if safety_on else []), 'x']), False)
                dwn.gender_mode = 'model'
                posts = _db_posts_json()
                dwn.items_raw_per_task = dwn._get_all_post_tags(posts)
                dwn._extract_cur_task_infos(set())
                ids = [dwn._extract_id(h) for h in dwn.items_raw_per_task]
                unknown_ids = [i for i in ids if str(infer_from_tags(dwn.item_info_dict_per_task[i].tags.split(), rules_for_module('db')).label) == 'unknown']
                self.assertTrue(unknown_ids, 'the fixture needs posts without gender tags')
                seen: list[list[str]] = []

                def fake_verdicts(_dwn, items, unknown_ids=unknown_ids, seen=seen):
                    seen.append([i for i, _ in items])
                    return {unknown_ids[0]: ModelVerdict(GR(GL.ONLY_FEMALE, H, source='model')),
                            **({unknown_ids[1]: ModelVerdict(GR(GL.ONLY_FEMALE, H, source='model'), safety_tag='loli')} if len(unknown_ids) > 1 else {})}
                with mock.patch('lynxx.gender_model.verdicts_for', fake_verdicts):
                    dwn._filter_items_by_gender(set())
                self.assertEqual(set(unknown_ids), set(seen[0]))  # only posts without conclusive tags go to the model
                kept = {dwn._extract_id(h) for h in dwn.items_raw_per_task}
                self.assertIn(unknown_ids[0], kept)
                self.assertEqual('model', dwn.gender_results[unknown_ids[0]].source)
                if len(unknown_ids) > 1:
                    self.assertEqual(not safety_on, unknown_ids[1] in kept)
                    self.assertEqual(1 if safety_on else 0, dwn.safety_blocked_count)

    @unittest.skipUnless(os.environ.get('LYNXX_GENDER_MODEL_DIR'), 'real model: set LYNXX_GENDER_MODEL_DIR to a folder with model.onnx')
    def test_real_model_on_the_app_icon(self) -> None:
        from lynxx.gender_model import GenderTagger, classify
        icon = (pathlib.Path(__file__).resolve().parents[2] / 'app/assets/icons/full-mask-orange.png').read_bytes()
        probs = GenderTagger.get().predict(icon)
        self.assertGreater(probs.get('1girl', 0), 0.5, sorted(probs.items(), key=lambda x: -x[1])[:10])
        self.assertEqual('only_female', str(classify(probs).result.label))

    def test_exclude_labels(self) -> None:
        # 'robot, no male only': every label but only_male is kept (none / unknown included)
        with _make_downloader(ProcModule.DB) as dwn:
            me = current_process()
            me.killed = False
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-gender_exclude', 'only_male', 'robot']), False)
            self.assertEqual(frozenset({'only_female', 'both', 'none', 'unknown'}), dwn.gender_select)
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-gender', 'only_female,only_male,both', '-gender_exclude', 'only_male', 'robot']), False)
            self.assertEqual(frozenset({'only_female', 'both'}), dwn.gender_select)
        with self.assertRaises(SystemExit):  # nothing would be left
            prepare_arglist(['-module', 'db', '-gender_exclude', 'only_female,only_male,both,none,unknown', 'x'])

    def test_preview_never_returns_blocked_posts(self) -> None:  # AC-S02
        from lynxx import server
        page = HTMLContent(None, _db_posts_json())  # type: ignore[arg-type]
        with mock.patch('lynxx.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': ['-gender', 'only_female', '-safety_blacklist']})
        ids = [it['id'] for it in res['items']]
        self.assertEqual(2, res['blockedCount'])
        self.assertNotIn('5', ids)
        self.assertNotIn('6', ids)
        self.assertNotIn('/5.jpg', json.dumps(res))
        by_id = {it['id']: it for it in res['items']}
        self.assertFalse(by_id['1']['excluded'])
        self.assertEqual(['gender'], by_id['2']['excludedBy'])
        self.assertEqual('only_female', by_id['1']['gender']['label'])
        with mock.patch('lynxx.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res2 = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': []})
        self.assertEqual(7, len(res2['items']))  # no filter -> no blacklist, unchanged behavior (product decision)
        self.assertFalse(res2['genderEnabled'])
        # the gender filter alone hides nothing for safety; the blacklist alone does, without any gender label
        with mock.patch('lynxx.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res3 = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': ['-gender', 'only_female']})
        self.assertEqual((0, 7), (res3['blockedCount'], len(res3['items'])))
        with mock.patch('lynxx.download_db.DownloaderDb.fetch_html', return_value=page), mock.patch.object(server, 'notify'):
            res4 = server.m_search_preview({'module': 'db', 'tags': 'scenery', 'args': ['-safety_blacklist']})
        self.assertEqual((2, 5), (res4['blockedCount'], len(res4['items'])))
        self.assertFalse(res4['genderEnabled'])
        self.assertNotIn('/6.jpg', json.dumps(res4))


class ExtensionGenderManifestTests(TestCase):
    def test_extension_rules_and_safety_tags(self) -> None:  # AC-S03 (extension adds tags)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict('os.environ', {'LYNXX_DATA_DIR': tmp}):
            Extensions.ensure_loaded()
            src = pathlib.Path(tmp) / 'src' / 'gsite'
            src.mkdir(parents=True)
            (src / 'gender_rules.json').write_text(json.dumps({'completeness': 0.95, 'female': {'high': ['onna']}, 'male': {'high': ['otoko']}}))
            (src / 'manifest.json').write_text(json.dumps({
                'id': 'gsite', 'name': 'G', 'abbr': 'gq', 'version': '1.0.0', 'entry': 'downloader.py:DownloaderGq',
                'genderRules': 'gender_rules.json', 'safetyTags': ['site_only_minor_tag']}))
            (src / 'downloader.py').write_text('from lynxx.download_db import DownloaderDb\n\n\nclass DownloaderGq(DownloaderDb):\n'
                                               "    def _get_module_abbr(self) -> str:\n        return 'gq'\n")
            Extensions.install_from_dir(src)
            self.assertTrue(safety.is_blocked(['site_only_minor_tag']))
            res = infer_from_tags(['onna'], rules_for_module('gq'))
            self.assertEqual((F, H), (res.label, res.conf))
            with _make_downloader(ProcModule.PROC_MODULES_BY_NAME['gq']) as dwn:
                self.assertIn('gender_tags', dwn.CAPABILITIES)
            Extensions.uninstall('gsite')

#
#
#########################################
