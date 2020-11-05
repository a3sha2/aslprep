# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
utils code to process GE scan with fewer volumes
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

"""

from pathlib import Path
import nibabel as nb
import numpy as np
import os 
import pandas as pd
from nipype.utils.filemanip import fname_presuffix
from ...niworkflows.func.util import init_enhance_and_skullstrip_asl_wf
from ...niworkflows.engine.workflows import LiterateWorkflow as Workflow
from ...niworkflows.interfaces.masks import SimpleShowMaskRPT 
from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu
from ... import config
DEFAULT_MEMORY_MIN_GB = config.DEFAULT_MEMORY_MIN_GB
LOGGER = config.loggers.workflow


def init_asl_geref_wf(omp_nthreads,mem_gb,metadata,bids_dir,brainmask_thresh=0.5,pre_mask=False, name="asl_gereference_wf",
    gen_report=False):

    workflow = Workflow(name=name)
    workflow.__desc__ = """
         First, a reference volume and its skull-stripped version were generated.
        """
    
    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "asl_file"
            ]
        ),
        name="inputnode",
    )

    outputnode = pe.Node(
        niu.IdentityInterface(
            fields=[
                "raw_ref_image",
                "ref_image",
                "ref_image_brain",
                "asl_mask",
                "mask_report",
            ]
        ),
        name="outputnode",
    )

    gen_ref = pe.Node(GeReferenceFile(bids_dir=bids_dir, in_metadata=metadata),
               omp_nthreads=omp_nthreads,mem_gb=mem_gb,name='gen_ge_ref',
                     run_without_submitting=False)
    skull_strip_wf = init_enhance_and_skullstrip_asl_wf(brainmask_thresh=0.5,name='skul_strip',pre_mask=False)
    mask_reportlet = pe.Node(SimpleShowMaskRPT(), name="mask_reportlet")

    workflow.connect([
         (inputnode,gen_ref,[('asl_file','input_image')]),
         (gen_ref,skull_strip_wf,[('out_file','inputnode.in_file')]),
         (gen_ref, outputnode, [
            ("out_file", "raw_ref_image"),]),
        (skull_strip_wf, outputnode, [
            ("outputnode.mask_file", "asl_mask"),
            ("outputnode.skull_stripped_file", "ref_image_brain")]),
         (skull_strip_wf, mask_reportlet, [
                ("outputnode.mask_file", "mask_file")]),
         (gen_ref,mask_reportlet,[("out_file", "background_file")]),
         ])
    return workflow



def init_asl_gereg_wf(use_bbr,asl2t1w_dof,asl2t1w_init,
        mem_gb, omp_nthreads, name='asl_reg_wf',
        sloppy=False, use_compression=True, write_report=True):
    
    workflow = Workflow(name=name)
    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=['ref_asl_brain', 't1w_brain', 't1w_dseg']),name='inputnode')
    outputnode = pe.Node(
        niu.IdentityInterface(fields=[
            'itk_asl_to_t1', 'itk_t1_to_asl', 'fallback']),name='outputnode')
    from .asl.registration import init_fsl_bbr_wf

    bbr_wf = init_fsl_bbr_wf(use_bbr=use_bbr, asl2t1w_dof=asl2t1w_dof,
                                 asl2t1w_init=asl2t1w_init, sloppy=sloppy)
    from ...interfaces import DerivativesDataSink

    workflow.connect([
        (inputnode, bbr_wf, [
            ('ref_asl_brain', 'inputnode.in_file'),
            ('t1w_dseg', 'inputnode.t1w_dseg'),
            ('t1w_brain', 'inputnode.t1w_brain')]),
        (bbr_wf, outputnode, [('outputnode.itk_asl_to_t1', 'itk_asl_to_t1'),
                              ('outputnode.itk_t1_to_asl', 'itk_t1_to_asl'),
                              ('outputnode.fallback', 'fallback')]),
    ])

    if write_report:
        ds_report_reg = pe.Node(
            DerivativesDataSink(datatype="figures", dismiss_entities=("echo",)),
            name='ds_report_reg', run_without_submitting=True,
            mem_gb=DEFAULT_MEMORY_MIN_GB)

        def _asl_reg_suffix(fallback):
            if fallback:
                return 'coreg' 

        workflow.connect([
            (bbr_wf, ds_report_reg, [
                ('outputnode.out_report', 'in_file'),
                (('outputnode.fallback', _asl_reg_suffix), 'desc')]),
        ])

    return workflow

def init_asl_t1_getrans_wf(mem_gb, omp_nthreads, cbft1space=False,
                          use_compression=True, name='asl_t1_trans_wf'):
    """
    Co-register the reference ASL image to T1w-space.

    The workflow uses :abbr:`BBR (boundary-based registration)`.

    

    """
    from ...niworkflows.engine.workflows import LiterateWorkflow as Workflow
    from ...niworkflows.func.util import init_asl_reference_wf
    from ...niworkflows.interfaces.fixes import FixHeaderApplyTransforms as ApplyTransforms
    from ...niworkflows.interfaces.itk import MultiApplyTransforms
    from ...niworkflows.interfaces.nilearn import Merge
    from ...niworkflows.interfaces.utils import GenerateSamplingReference

    workflow = Workflow(name=name)
    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=['name_source', 'ref_asl_brain', 'ref_asl_mask','asl_file',
                    't1w_brain', 't1w_mask', 't1w_aseg','cbf', 'meancbf','att',
                    'score', 'avgscore', 'scrub', 'basil', 'pv', 'itk_asl_to_t1']),
        name='inputnode'
    )

    outputnode = pe.Node(
        niu.IdentityInterface(fields=[
            'asl_t1', 'asl_t1_ref', 'asl_mask_t1','att_t1','cbf_t1', 'meancbf_t1', 
            'score_t1', 'avgscore_t1', 'scrub_t1', 'basil_t1', 'pv_t1']),
        name='outputnode'
    )

    gen_ref = pe.Node(GenerateSamplingReference(), name='gen_ref',
                      mem_gb=0.3)  # 256x256x256 * 64 / 8 ~ 150MB

    mask_t1w_tfm = pe.Node(ApplyTransforms(interpolation='MultiLabel'),
                           name='mask_t1w_tfm', mem_gb=0.1)

    workflow.connect([
        (inputnode, gen_ref, [('ref_asl_brain', 'moving_image'),
                              ('t1w_brain', 'fixed_image'),
                              ('t1w_mask', 'fov_mask')]),
        (inputnode, mask_t1w_tfm, [('ref_asl_mask', 'input_image')]),
        (gen_ref, mask_t1w_tfm, [('out_file', 'reference_image')]),
        (inputnode, mask_t1w_tfm, [('itk_asl_to_t1', 'transforms')]),
        (mask_t1w_tfm, outputnode, [('output_image', 'asl_mask_t1')]),
    ])

    asl_to_t1w_transform = pe.Node(
                  ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
                  name='asl_to_t1w_transform', mem_gb=mem_gb)
   
    # Generate a reference on the target T1w space
   
    
    workflow.connect([
            (inputnode, asl_to_t1w_transform, [('asl_file', 'input_image')]),
            (inputnode, asl_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
            (inputnode, asl_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),
            (asl_to_t1w_transform, outputnode, [('output_image', 'asl_t1')]),
        ])

    if cbft1space:
        
        
        cbf_to_t1w_transform = pe.Node(
               ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
              name='cbf_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        meancbf_to_t1w_transform = pe.Node(
                         ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
                         name='meancbf_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        score_to_t1w_transform = pe.Node(
             ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
             name='score_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        avgscore_to_t1w_transform = pe.Node(
            ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
           name='avgscore_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        scrub_to_t1w_transform = pe.Node(
              ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
              name='scrub_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        basil_to_t1w_transform = pe.Node(
               ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
            name='basil_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        pv_to_t1w_transform = pe.Node(
               ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
               name='pv_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        att_to_t1w_transform = pe.Node(
               ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
               name='att_to_t1w_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
        workflow.connect([
         
        (asl_to_t1w_transform, outputnode, [('output_image', 'asl_t1_ref')]),

        (inputnode, cbf_to_t1w_transform, [('cbf', 'input_image')]),
        (cbf_to_t1w_transform, outputnode, [('output_image', 'cbf_t1')]),
        (inputnode, cbf_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, cbf_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, score_to_t1w_transform, [('score', 'input_image')]),
        (score_to_t1w_transform, outputnode, [('output_image', 'score_t1')]),
        (inputnode, score_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, score_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, meancbf_to_t1w_transform, [('meancbf', 'input_image')]),
        (meancbf_to_t1w_transform, outputnode, [('output_image', 'meancbf_t1')]),
        (inputnode, meancbf_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, meancbf_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, avgscore_to_t1w_transform, [('avgscore', 'input_image')]),
        (avgscore_to_t1w_transform, outputnode, [('output_image', 'avgscore_t1')]),
        (inputnode, avgscore_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, avgscore_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, scrub_to_t1w_transform, [('scrub', 'input_image')]),
        (scrub_to_t1w_transform, outputnode, [('output_image', 'scrub_t1')]),
        (inputnode, scrub_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, scrub_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, basil_to_t1w_transform, [('basil', 'input_image')]),
        (basil_to_t1w_transform, outputnode, [('output_image', 'basil_t1')]),
        (inputnode, basil_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, basil_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, pv_to_t1w_transform, [('pv', 'input_image')]),
        (pv_to_t1w_transform, outputnode, [('output_image', 'pv_t1')]),
        (inputnode, pv_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, pv_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),

        (inputnode, att_to_t1w_transform, [('att', 'input_image')]),
        (att_to_t1w_transform, outputnode, [('output_image', 'att_t1')]),
        (inputnode, att_to_t1w_transform, [('itk_asl_to_t1', 'transforms')]),
        (inputnode, att_to_t1w_transform, [('ref_asl_brain', 'reference_image')]),
         ])

    return workflow


def init_asl_gestd_trans_wf(
    mem_gb,
    omp_nthreads,
    spaces,
    name='asl_gestd_trans_wf',
    use_compression=True,
):
    """

    """
    from ...niworkflows.engine.workflows import LiterateWorkflow as Workflow
    from ...niworkflows.func.util import init_asl_reference_wf
    from ...niworkflows.interfaces.fixes import FixHeaderApplyTransforms as ApplyTransforms
    from ...niworkflows.interfaces.itk import MultiApplyTransforms
    from ...niworkflows.interfaces.utility import KeySelect
    from ...niworkflows.interfaces.utils import GenerateSamplingReference
    from ...niworkflows.interfaces.nilearn import Merge
    from ...niworkflows.utils.spaces import format_reference

    workflow = Workflow(name=name)
    output_references = spaces.cached.get_spaces(nonstandard=False, dim=(3,))
    std_vol_references = [
        (s.fullname, s.spec) for s in spaces.references if s.standard and s.dim == 3
    ]

    if len(output_references) == 1:
        workflow.__desc__ = """\
The ASL and CBF dreivatives  were resampled into standard space,
generating a *preprocessed ASL and computed CBF in {tpl} space*.
""".format(tpl=output_references[0])
    elif len(output_references) > 1:
        workflow.__desc__ = """\
The ASL and CBF dreivatives were resampled into several standard spaces,
correspondingly generating the following *spatially-normalized,
preprocessed ASL runs*: {tpl}.
""".format(tpl=', '.join(output_references))

    inputnode = pe.Node(
        niu.IdentityInterface(fields=[
            'anat2std_xfm','raw_ref_asl',
            'cbf','meancbf','att','asl_file',
            'score','avgscore','scrub',
            'basil','pv','asl_mask',
            'itk_asl_to_t1',
            'name_source','templates',
        ]),
        name='inputnode'
    )

    iterablesource = pe.Node(
        niu.IdentityInterface(fields=['std_target']), name='iterablesource'
    )
    # Generate conversions for every template+spec at the input
    iterablesource.iterables = [('std_target', std_vol_references)]

    split_target = pe.Node(niu.Function(
        function=_split_spec, input_names=['in_target'],
        output_names=['space', 'template', 'spec']),
        run_without_submitting=True, name='split_target')

    select_std = pe.Node(KeySelect(fields=['anat2std_xfm']),
                         name='select_std', run_without_submitting=True)

    select_tpl = pe.Node(niu.Function(function=_select_template),
                         name='select_tpl', run_without_submitting=True)

    

    mask_std_tfm = pe.Node(ApplyTransforms(interpolation='MultiLabel'),
                           name='mask_std_tfm', mem_gb=1)

    # Write corrected file in the designated output dir
    mask_merge_tfms = pe.Node(niu.Merge(2), name='mask_merge_tfms', run_without_submitting=True,
                              mem_gb=DEFAULT_MEMORY_MIN_GB)
    nxforms = 3 
    merge_xforms = pe.Node(niu.Merge(nxforms), name='merge_xforms',
                           run_without_submitting=True, mem_gb=DEFAULT_MEMORY_MIN_GB)

    asl_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
        name='asl_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
    cbf_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
        name='cbf_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    score_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True, input_image_type=3,
                        dimension=3),
        name='score_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    meancbf_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='meancbf_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    avgscore_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='avgscore_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    scrub_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='scrub_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    basil_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='basil_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    pv_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='pv_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)
    att_to_std_transform = pe.Node(
        ApplyTransforms(interpolation="LanczosWindowedSinc", float=True),
        name='att_to_std_transform', mem_gb=mem_gb * 3 * omp_nthreads, n_procs=omp_nthreads)

    #merge = pe.Node(Merge(compress=use_compression), name='merge',
                    #mem_gb=mem_gb * 3)
    mask_merge_tfms = pe.Node(niu.Merge(2), name='mask_merge_tfms', run_without_submitting=True,
                              mem_gb=DEFAULT_MEMORY_MIN_GB)
    # Generate a reference on the target standard space
    gen_ref = pe.Node(GenerateSamplingReference(), name='gen_ref',
                      mem_gb=0.3) 

    workflow.connect([
        (iterablesource, split_target, [('std_target', 'in_target')]),
        (iterablesource, select_tpl, [('std_target', 'template')]),
        (inputnode, select_std, [('anat2std_xfm', 'anat2std_xfm'),
                                 ('templates', 'keys')]),
        (inputnode, mask_std_tfm, [('asl_mask', 'input_image')]),
        (inputnode, gen_ref, [('asl_file', 'moving_image')]),
        (inputnode, merge_xforms, [
            (('itk_asl_to_t1', _aslist), 'in2')]),
        (inputnode, mask_merge_tfms, [(('itk_asl_to_t1', _aslist), 'in2')]),
        (inputnode, asl_to_std_transform, [('asl_file', 'input_image')]),
        (split_target, select_std, [('space', 'key')]),
        (select_std, merge_xforms, [('anat2std_xfm', 'in1')]),
        (select_std, mask_merge_tfms, [('anat2std_xfm', 'in1')]),
        (split_target, gen_ref, [(('spec', _is_native), 'keep_native')]),
        (select_tpl, gen_ref, [('out', 'fixed_image')]),
        (merge_xforms, asl_to_std_transform, [('out', 'transforms')]),
        (gen_ref, asl_to_std_transform, [('out_file', 'reference_image')]),
        (gen_ref, mask_std_tfm, [('out_file', 'reference_image')]),
        (mask_merge_tfms, mask_std_tfm, [('out', 'transforms')])
    ])

    output_names = [
        'asl_mask_std',
        'asl_std',
        'asl_std_ref',
        'spatial_reference',
        'template',
        'cbf_std',
        'meancbf_std',
        'score_std',
        'avgscore_std',
        'scrub_std',
        'basil_std',
        'pv_std',
        'att_std',
    ] 

    poutputnode = pe.Node(niu.IdentityInterface(fields=output_names),
                          name='poutputnode')
    workflow.connect([
        # Connecting outputnode
        (iterablesource, poutputnode, [
            (('std_target', format_reference), 'spatial_reference')]),
        (asl_to_std_transform, poutputnode, [('output_image', 'asl_std')]),
        (asl_to_std_transform, poutputnode, [('output_image', 'asl_std_ref')]),
        (mask_std_tfm, poutputnode, [('output_image', 'asl_mask_std')]),
        (select_std, poutputnode, [('key', 'template')]),

        (mask_merge_tfms, cbf_to_std_transform, [('out', 'transforms')]),
        (inputnode, cbf_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, cbf_to_std_transform, [('cbf', 'input_image')]),
        (cbf_to_std_transform, poutputnode, [('output_image', 'cbf_std')]),

        (mask_merge_tfms, score_to_std_transform, [('out', 'transforms')]),
        (inputnode, score_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, score_to_std_transform, [('score', 'input_image')]),
        (score_to_std_transform, poutputnode, [('output_image', 'score_std')]),

        (mask_merge_tfms, meancbf_to_std_transform, [('out', 'transforms')]),
        (inputnode, meancbf_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, meancbf_to_std_transform, [('cbf', 'input_image')]),
        (meancbf_to_std_transform, poutputnode, [('output_image', 'meancbf_std')]),

        (mask_merge_tfms, avgscore_to_std_transform, [('out', 'transforms')]),
        (inputnode, avgscore_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, avgscore_to_std_transform, [('avgscore', 'input_image')]),
        (avgscore_to_std_transform, poutputnode, [('output_image', 'avgscore_std')]),

        (mask_merge_tfms, scrub_to_std_transform, [('out', 'transforms')]),
        (inputnode, scrub_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, scrub_to_std_transform, [('scrub', 'input_image')]),
        (scrub_to_std_transform, poutputnode, [('output_image', 'scrub_std')]),

        (mask_merge_tfms, basil_to_std_transform, [('out', 'transforms')]),
        (inputnode, basil_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, basil_to_std_transform, [('basil', 'input_image')]),
        (basil_to_std_transform, poutputnode, [('output_image', 'basil_std')]),

        (mask_merge_tfms, pv_to_std_transform, [('out', 'transforms')]),
        (inputnode, pv_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, pv_to_std_transform, [('pv', 'input_image')]),
        (pv_to_std_transform, poutputnode, [('output_image', 'pv_std')]),

        (mask_merge_tfms, att_to_std_transform, [('out', 'transforms')]),
        (inputnode, att_to_std_transform, [('raw_ref_asl', 'reference_image')]),
        (inputnode, att_to_std_transform, [('att', 'input_image')]),
        (att_to_std_transform, poutputnode, [('output_image', 'att_std')]),
    ])
    # Connect parametric outputs to a Join outputnode
    outputnode = pe.JoinNode(niu.IdentityInterface(fields=output_names),
                             name='outputnode', joinsource='iterablesource')
    workflow.connect([
        (poutputnode, outputnode, [(f, f) for f in output_names]),
    ])
    return workflow

from nipype.interfaces.base import (
    traits,
    isdefined,
    File,
    InputMultiPath,
    TraitedSpec,
    BaseInterfaceInputSpec,
    SimpleInterface,
    DynamicTraitedSpec,
)

class _GenerateReferenceInputSpec(BaseInterfaceInputSpec):
    input_image = File(
        exists=True, mandatory=True, desc="input images"
    )
    

class _GenerateReferenceOutputSpec(TraitedSpec):
    out_file = File(exists=True, desc="one file with all inputs flattened")


class GenerateReference(SimpleInterface):
    """
    Generates a reference grid for resampling one image keeping original resolution,
    but moving data to a different space (e.g. MNI).

    """

    input_spec = _GenerateReferenceInputSpec
    output_spec = _GenerateReferenceOutputSpec

    def _run_interface(self, runtime):
        self._results["out_file"] = gen_reference(
            in_img=self.inputs.input_image)
        return runtime




def gen_reference(in_img, newpath=None):

    """generate reference for a GE scan with few volumes."""
    import nibabel as nb 
    import numpy as np
    import os 
    newpath=os.getcwd()
    newpath = Path(newpath or ".")
    ss=check_img(in_img)
    if ss == 0: 
        ref_data=nb.load(in_img).get_fdata()
    else: 
        nii = nb.load(in_img).get_fdata()
        ref_data=np.mean(nii,axis=3)
    
    new_file = nb.Nifti1Image(dataobj=ref_data,header=nb.load(in_img).header,
             affine=nb.load(in_img).affine)
    out_file = fname_presuffix('aslref', suffix="_reference", newpath=str(newpath.absolute()))
    new_file.to_filename(out_file)
    return out_file

def check_img(img):
    # get the 4th dimension 
    ss=nb.load(img).get_fdata().shape
    if len(ss) == 3:
        ss=np.hstack([ss,0])
    return ss[3]

def _split_spec(in_target):
    space, spec = in_target
    template = space.split(':')[0]
    return space, template, spec


def _select_template(template):
    from ...niworkflows.utils.misc import get_template_specs
    template, specs = template
    template = template.split(':')[0]  # Drop any cohort modifier if present
    specs = specs.copy()
    specs['suffix'] = specs.get('suffix', 'T1w')
def _first(inlist):
    return inlist[0]


def _aslist(in_value):
    if isinstance(in_value, list):
        return in_value
    return [in_value]


def _is_native(in_value):
    return (
        in_value.get('resolution') == 'native'
        or in_value.get('res') == 'native'
    )


class _GeReferenceFileInputSpec(BaseInterfaceInputSpec):
    input_image = File(
        exists=True, mandatory=True, desc="asl_file"
    )
    in_metadata = traits.Dict(exists=True, mandatory=True,
                              desc='metadata for asl or deltam ')
    bids_dir=traits.Str(exits=True,mandatory=True,desc=' bids directory')
    

class _GeReferenceFileOutputSpec(TraitedSpec):
    out_file = File(exists=True, desc="one file with all inputs flattened")


class GeReferenceFile(SimpleInterface):
    """
    Generates a reference grid for resampling one image keeping original resolution,
    but moving data to a different space (e.g. MNI).

    """

    input_spec = _GeReferenceFileInputSpec
    output_spec = _GeReferenceFileOutputSpec

    def _run_interface(self, runtime):
        import os 
        filex = os.path.abspath(self.inputs.in_file)
        if self.inputs.in_metadata['M0'] != "True" and self.inputs.in_metadata['M0'] != "False" and type(self.inputs.in_metadata['M0']) != int :
           m0file=os.path.abspath(self.inputs.bids_dir+'/'+self.inputs.in_metadata['M0'])
           #m0file_metadata=readjson(m0file.replace('nii.gz','json'))
           #aslfile_linkedM0 = os.path.abspath(self.inputs.bids_dir+'/'+m0file_metadata['IntendedFor'])
           aslcontext1 = filex.replace('_asl.nii.gz', '_aslcontext.tsv')
           aslcontext = pd.read_csv(aslcontext1)
           idasl = aslcontext['volume_type'].tolist()
           m0list = [i for i in range(0, len(idasl)) if idasl[i] == 'm0scan']
           deltamlist = [i for i in range(0, len(idasl)) if idasl[i] == 'deltam']
           cbflist = [i for i in range(0, len(idasl)) if idasl[i] == 'CBF']

           allasl = nb.load(self.inputs.asl_file)
           dataasl = allasl.get_fdata()
    #get reference file from m0 or mean of delta or CBF 
    

        if m0file: 
            reffile = gen_reference(m0file)
        elif len(dataasl.shape) > 3:
            if m0list > 0:
                modata2 = dataasl[:, :, :, m0list]
                m0filename=fname_presuffix(self.inputs.in_file,
                                                    suffix='_mofile', newpath=os.get_cwd())
                m0obj = nb.Nifti1Image(modata2, allasl.affine, allasl.header)
                m0obj.to_filename(m0filename)
                reffile = gen_reference(m0filename)
            elif deltamlist > 0 :
                modata2 = dataasl[:, :, :, deltamlist]
                m0filename=fname_presuffix(self.inputs.in_file,
                                                    suffix='_mofile', newpath=os.get_cwd())
                m0obj = nb.Nifti1Image(modata2, allasl.affine, allasl.header)
                m0obj.to_filename(m0filename)
                reffile = gen_reference(m0filename)
            elif cbflist > 0 : 
                modata2 = dataasl[:, :, :, cbflist]
                m0filename=fname_presuffix(self.inputs.in_file,
                                                    suffix='_mofile', newpath=os.get_cwd())
                m0obj = nb.Nifti1Image(modata2, allasl.affine, allasl.header)
                m0obj.to_filename(m0filename)
                reffile = gen_reference(m0filename)
        else:
            reffile=gen_reference(self.inputs.in_file)
        self.inputs.out_file = os.path.abspath(reffile)
        return runtime


def readjson(jsonfile):
    import json
    with open(jsonfile) as f:
        data = json.load(f)
    return data