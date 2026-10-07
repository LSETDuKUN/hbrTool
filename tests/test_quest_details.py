import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from hbr_data.assets import AssetStore, asset_urls, sync_portraits, asset_lock
from hbr_data.details import skill_detail
from hbr_data.cards import ratios


class DetailTests(unittest.TestCase):
    def test_missing_hit_distribution_is_not_inferred(self):
        skill = skill_detail({"name":"X","hit_count":2,"parts":[{"power":[100,200],"parameters":{"str":2,"dex":1}}]})
        self.assertEqual(skill.hits,())
        self.assertEqual(skill.effects[0].hits,())
        self.assertIsNone(skill.effects[0].hp_multiplier)
        self.assertEqual(skill.effects[0].power,(100,200))

    def test_skill_and_effect_hit_distributions_stay_separate(self):
        value={"hit_count":2,"hits":[{"id":1,"type":"Main","power_ratio":.3},{"id":2,"type":"Main","power_ratio":.7}],
               "parts":[{"hits":[{"id":1,"type":"Before","power_ratio":0}],"multipliers":{"dr":1.5}}]}
        skill=skill_detail(value)
        self.assertEqual(ratios(skill.hits),'0.3 + 0.7')
        self.assertEqual(skill.effects[0].hits[0].kind,'Before')
        self.assertEqual(skill.effects[0].destruction_multiplier,1.5)
        self.assertEqual(value['hits'][0]['power_ratio'],.3)

    def test_portrait_paths_reject_traversal(self):
        for filename in ('../x.webp','a/b.webp','a\\b.webp',''):
            self.assertEqual(asset_urls('jp','styles',filename),[])
        self.assertIn('/en/hbr/',asset_urls('en','styles','x.webp')[1])

    def test_shared_portrait_downloads_once_and_has_lookup_api(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);image=Image.new('RGBA',(16,16),'pink');buffer=io.BytesIO();image.save(buffer,format='WEBP')
            rows=[('jp','styles','1','x.webp'),('jp','styles','2','x.webp')]
            with patch('hbr_data.assets.Catalog') as catalog,patch('hbr_data.assets.fetch',return_value=buffer.getvalue()) as fetch:
                catalog.return_value.query.return_value=([],rows,False)
                catalog.return_value.manifest.return_value={'database':'fixture'}
                sync_portraits(root,lambda _:None)
                self.assertEqual(fetch.call_count,1)
                a,b=(AssetStore(root).get('jp','styles',i) for i in ('1','2'))
                self.assertEqual(a['local_path'],b['local_path'])
                self.assertTrue(Path(a['local_path']).exists())
                self.assertEqual(a['status'],'ready')
                sync_portraits(root,lambda _:None)
                self.assertEqual(fetch.call_count,1)

    def test_bad_response_is_not_cached_as_an_image(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('hbr_data.assets.Catalog') as catalog,patch('hbr_data.assets.fetch',return_value=b'<html>error</html>'):
                catalog.return_value.query.return_value=([],[('jp','enemies','1','enemy.webp')],False)
                catalog.return_value.manifest.return_value={'database':'fixture'}
                sync_portraits(temp,lambda _:None)
                result=AssetStore(temp).get('jp','enemies','1')
                self.assertEqual(result['status'],'error')
                self.assertIsNone(result['local_path'])

    def test_lock_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError,'test'):
                with asset_lock(Path(temp)):
                    raise ValueError('test')
            with asset_lock(Path(temp)):
                pass
