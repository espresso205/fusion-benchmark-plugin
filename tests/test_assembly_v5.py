import importlib.util
import unittest

AVAILABLE=importlib.util.find_spec('src.assembly_v5') is not None
if AVAILABLE: from src.assembly_v5 import match_parts, basic_shape, inspect_interference

def part(name,volume,center):
    return {'id':name,'volume_mm3':volume,'area_mm2':6*volume**(2/3),
            'principal_moments':[volume**(5/3)/6]*3,'centroid_mm':center}

class MatchingTests(unittest.TestCase):
    def setUp(self): self.assertTrue(AVAILABLE,'v5 instance matching must exist')
    def test_small_missing_part_counts_as_missing(self):
        gt=[part('body',1000,[0,0,0]),part('washer',1,[0,0,10])]
        result=match_parts(gt,gt[:1])
        self.assertEqual(result['missing'],['washer'])
        self.assertEqual(result['part_count_coverage'],.5)
    def test_duplicate_and_reordered_instances_are_preserved(self):
        gt=[part('one',1,[0,0,0]),part('two',1,[0,0,5])]
        self.assertEqual(match_parts(gt,list(reversed(gt)))['missing'],[])
        result=match_parts(gt,[*gt,part('extra',1,[0,0,5])])
        self.assertEqual(len(result['extra']),1)
        self.assertEqual(len(result['duplicate_candidates']),1)
    def test_moved_part_is_not_individually_realigned(self):
        gt=[part('one',1,[0,0,0]),part('two',1,[0,0,5])]
        candidate=[gt[0],part('moved',1,[0,0,20])]
        result=match_parts(gt,candidate)
        self.assertEqual(max(r['centroid_error_mm'] for r in result['matches']),15.)
    def test_unrelated_shape_can_remain_unmatched(self):
        result=match_parts([part('small',1,[0,0,0])],[part('large',100,[0,0,0])])
        self.assertEqual(result['missing'],['small'])
        self.assertEqual(result['extra'],['large'])
    def test_exact_bbox_sweep_retains_containment_and_rejects_touching(self):
        import src.assembly_v5 as core
        self.assertTrue(hasattr(core,'bbox_overlap_pairs'),'exact sweep index required')
        boxes=[[0,0,0,10,10,10],[1,1,1,2,2,2],[10,0,0,11,1,1],[0,20,0,1,21,1]]
        self.assertEqual(core.bbox_overlap_pairs(boxes),[(1,0)])

class GeometryTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(AVAILABLE,'v5 geometry checks must exist')
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.gp import gp_Pnt
        self.box=lambda x,y,z,dx,dy,dz:BRepPrimAPI_MakeBox(gp_Pnt(x,y,z),dx,dy,dz).Shape()
    def test_basic_passes_overlapping_solids_strict_fails(self):
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        a=self.box(0,0,0,2,2,2); b=self.box(1,1,1,2,2,2)
        builder=BRep_Builder(); compound=TopoDS_Compound();builder.MakeCompound(compound)
        builder.Add(compound,a);builder.Add(compound,b)
        self.assertEqual(basic_shape(compound,['mm'])['status'],'pass')
        self.assertEqual(inspect_interference([a,b])['status'],'fail')
    def test_touching_gap_and_containment(self):
        a=self.box(0,0,0,2,2,2)
        for x in (2,2.0001):
            self.assertEqual(inspect_interference([a,self.box(x,0,0,1,1,1)])['status'],'pass')
        result=inspect_interference([a,self.box(.5,.5,.5,.5,.5,.5)])
        self.assertEqual(result['status'],'fail')
        self.assertAlmostEqual(result['raw']['issues'][0]['intersection_volume_mm3'],.125)

    def test_xcaf_physical_coincident_instances_are_not_collapsed(self):
        import tempfile
        from pathlib import Path
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.TopLoc import TopLoc_Location
        from src.assembly_v5 import load_instances
        doc=TDocStd_Document(TCollection_ExtendedString('test'))
        tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        parent=tool.NewShape();part_label=tool.AddShape(self.box(0,0,0,1,1,1),False)
        tool.AddComponent(parent,part_label,TopLoc_Location());tool.AddComponent(parent,part_label,TopLoc_Location())
        tool.UpdateAssemblies()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'duplicate.step';writer=STEPCAFControl_Writer()
            writer.Transfer(doc,STEPControl_AsIs);writer.Write(str(path))
            instances=load_instances(path)
            self.assertEqual(len(instances),2)
            self.assertNotEqual(instances[0]['id'],instances[1]['id'])

    def test_frozen_gap_and_position_rules_detect_moved_part(self):
        import src.assembly_v5 as core
        self.assertTrue(hasattr(core,'evaluate_fit'),'frozen fit evaluator required')
        instances=[{'id':'a','shape':self.box(0,0,0,1,1,1)}, {'id':'b','shape':self.box(3,0,0,1,1,1)}]
        matching={'matches':[{'reference_id':'A','candidate_id':'a','centroid_error_mm':0},
                             {'reference_id':'B','candidate_id':'b','centroid_error_mm':2}]}
        rules={'position_tolerance_mm':.1,'fits':[{'id':'joint','type':'surface_gap',
                'reference_ids':['A','B'],'minimum_mm':0,'maximum_mm':.1}]}
        result=core.evaluate_fit(instances,matching,rules)
        self.assertEqual(result['status'],'fail')
        self.assertEqual(result['raw']['checks'][-1]['measured_gap_mm'],2.)
        self.assertEqual(core.evaluate_fit(instances,matching,None)['status'],'not_run')

    def test_interference_exemption_is_local_and_volume_bounded(self):
        import src.assembly_v5 as core
        self.assertTrue(hasattr(core,'allowed_interference'),'bounded fit exemption required')
        issue={'entity_ids':['a','b'],'intersection_volume_mm3':.1,'bbox_mm':[0,0,0,1,1,1]}
        rule={'candidate_ids':['a','b'],'maximum_volume_mm3':.2,'allowed_bbox_mm':[0,0,0,1,1,1]}
        self.assertTrue(core.allowed_interference(issue,rule))
        self.assertFalse(core.allowed_interference({**issue,'intersection_volume_mm3':.3},rule))
        self.assertFalse(core.allowed_interference({**issue,'entity_ids':['a','c']},rule))
        self.assertFalse(core.allowed_interference({**issue,'bbox_mm':[0,0,0,2,1,1]},rule))

    def test_annulus_rejects_corner_inside_bounding_box(self):
        import src.assembly_v5 as core
        issue={'entity_ids':['a','b'],'intersection_volume_mm3':.008,'bbox_mm':[.8,.8,0,1,1,.2]}
        rule={'candidate_ids':['a','b'],'maximum_volume_mm3':.02,
              'allowed_annulus':{'axis_origin_mm':[0,0,0],'axis_direction':[0,0,1],
                                 'length_mm':1,'inner_radius_mm':0,'outer_radius_mm':1}}
        self.assertFalse(core.allowed_interference(issue,rule,self.box(.8,.8,0,.2,.2,.2)))
        inside={**issue,'bbox_mm':[.1,.1,0,.3,.3,.2]}
        self.assertTrue(core.allowed_interference(inside,rule,self.box(.1,.1,0,.2,.2,.2)))

    def test_multibody_allowance_caps_total_physical_pair(self):
        from src.assembly_v5 import inspect_interference
        solids=[self.box(x,0,0,2,2,2) for x in (0,10,1,11)]
        rule={'id':'A-B','candidate_ids':['A','B'],'maximum_volume_mm3':6.,
              'allowed_bbox_mm':[-1,-1,-1,20,4,4]}
        result=inspect_interference(solids,ids=['A0','A1','B0','B1'],physical_ids=['A','A','B','B'],allowances=[rule])
        self.assertEqual(result['status'],'fail')
        self.assertEqual(result['raw']['allowed_interferences'],[])
        self.assertAlmostEqual(result['raw']['issues'][0]['intersection_volume_mm3'],8.)
        self.assertEqual(len(result['raw']['issues'][0]['constituent_intersections']),2)
        result=inspect_interference(solids,ids=['A0','A1','B0','B1'],physical_ids=['A','A','B','B'],
                                    allowances=[{**rule,'maximum_volume_mm3':10.}])
        self.assertEqual(result['status'],'pass')
        self.assertEqual(len(result['raw']['allowed_interferences']),1)
        self.assertAlmostEqual(result['raw']['allowed_interferences'][0]['intersection_volume_mm3'],8.)
        self.assertEqual(result['raw']['allowed_interferences'][0]['maximum_volume_mm3'],10.)

    def test_pending_physical_allowance_not_accepted_before_all_bodies_complete(self):
        from src.assembly_v5 import inspect_interference
        import copy
        snapshots=[];calls=0
        def event(name,**info):
            nonlocal calls
            if name=='solid_common' and info['phase']=='start':
                calls+=1
                if calls==2:raise RuntimeError('simulated interruption')
        rule={'id':'A-B','candidate_ids':['A','B'],'maximum_volume_mm3':9.,'allowed_bbox_mm':[-1,-1,-1,20,4,4]}
        with self.assertRaisesRegex(RuntimeError,'interruption'):
            inspect_interference([self.box(x,0,0,2,2,2) for x in (0,10,1,11)],event,
                lambda x:snapshots.append(copy.deepcopy(x)),['A0','A1','B0','B1'],[rule],physical_ids=['A','A','B','B'])
        self.assertEqual(snapshots[-1]['raw']['allowed_interferences'],[])
        self.assertEqual(snapshots[-1]['raw']['unchecked_physical_pairs'],[['A','B']])
        self.assertEqual(len(snapshots[-1]['raw']['pending_interferences']),1)

    def test_fit_progress_precedes_expensive_call_and_counts_one_unit(self):
        from src.assembly_v5 import evaluate_fit
        snapshots=[]
        matching={'matches':[{'reference_id':'A','candidate_id':'a','centroid_error_mm':0},
                             {'reference_id':'B','candidate_id':'b','centroid_error_mm':0}]}
        rules={'position_tolerance_mm':.1,'fits':[{'id':'joint','type':'surface_gap','reference_ids':['A','B'],'minimum_mm':0,'maximum_mm':.1}]}
        def event(name,**data):
            if name=='fit_rule' and data['phase']=='start':
                self.assertTrue(snapshots,'completed positions must be saved before exact gap starts')
                self.assertEqual(snapshots[-1]['raw']['checked_count'],2)
                self.assertEqual(snapshots[-1]['raw']['total_count'],3)
                self.assertEqual(snapshots[-1]['raw']['unchecked_rules'],['joint'])
        result=evaluate_fit([{'id':'a','shape':self.box(0,0,0,1,1,1)},{'id':'b','shape':self.box(1,0,0,1,1,1)}],matching,rules,event,snapshots.append)
        self.assertEqual(result['raw']['checked_count'],3)
        self.assertEqual(result['raw']['total_count'],3)
        self.assertEqual(result['raw']['unchecked_rules'],[])

    def test_multibody_physical_leaf_is_one_instance_with_two_constituents(self):
        import tempfile
        from pathlib import Path
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.STEPControl import STEPControl_AsIs
        from OCP.TopLoc import TopLoc_Location
        from OCP.BRep import BRep_Builder
        from OCP.TopoDS import TopoDS_Compound
        from src.assembly_v5 import load_instances,descriptors
        builder=BRep_Builder();compound=TopoDS_Compound();builder.MakeCompound(compound)
        builder.Add(compound,self.box(0,0,0,1,1,1));builder.Add(compound,self.box(3,0,0,1,1,1))
        doc=TDocStd_Document(TCollection_ExtendedString('test'));tool=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        parent=tool.NewShape();part_label=tool.AddShape(compound,False)
        tool.AddComponent(parent,part_label,TopLoc_Location());tool.UpdateAssemblies()
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'multibody.step';writer=STEPCAFControl_Writer();writer.Transfer(doc,STEPControl_AsIs);writer.Write(str(path))
            instances=load_instances(path)
            self.assertEqual(len(instances),1)
            self.assertEqual(len(instances[0]['constituents']),2)
            self.assertAlmostEqual(descriptors(instances)[0]['volume_mm3'],2.)

if __name__=='__main__': unittest.main()
