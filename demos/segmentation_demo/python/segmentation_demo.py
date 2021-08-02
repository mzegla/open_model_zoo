#!/usr/bin/env python3
"""
 Copyright (C) 2018-2020 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import logging as log
import sys
from argparse import ArgumentParser, SUPPRESS
from pathlib import Path
from time import perf_counter

import cv2
import numpy as np
from openvino.inference_engine import IECore, get_version

sys.path.append(str(Path(__file__).resolve().parents[2] / 'common/python'))

from models import OutputTransform, SegmentationModel, SalientObjectDetectionModel
import monitors
from pipelines import get_user_config, parse_devices, AsyncPipeline
from images_capture import open_images_capture
from performance_metrics import PerformanceMetrics
from helpers import resolution, log_blobs_info, log_runtime_settings, log_latency_per_stage

log.basicConfig(format='[ %(levelname)s ] %(message)s', level=log.DEBUG, stream=sys.stdout)


class SegmentationVisualizer:
    pascal_voc_palette = [
        (0,   0,   0),
        (128, 0,   0),
        (0,   128, 0),
        (128, 128, 0),
        (0,   0,   128),
        (128, 0,   128),
        (0,   128, 128),
        (128, 128, 128),
        (64,  0,   0),
        (192, 0,   0),
        (64,  128, 0),
        (192, 128, 0),
        (64,  0,   128),
        (192, 0,   128),
        (64,  128, 128),
        (192, 128, 128),
        (0,   64,  0),
        (128, 64,  0),
        (0,   192, 0),
        (128, 192, 0),
        (0,   64,  128)
    ]

    def __init__(self, colors_path=None):
        if colors_path:
            self.color_palette = self.get_palette_from_file(colors_path)
            log.debug('The palette is loaded from {}'.format(colors_path))
        else:
            self.color_palette = self.pascal_voc_palette
            log.debug('The PASCAL VOC palette is used')
        log.debug('Get {} colors'.format(len(self.color_palette)))
        self.color_map = self.create_color_map()

    def get_palette_from_file(self, colors_path):
        with open(colors_path, 'r') as file:
            colors = []
            for line in file.readlines():
                values = line[line.index('(')+1:line.index(')')].split(',')
                colors.append([int(v.strip()) for v in values])
            return colors

    def create_color_map(self):
        classes = np.array(self.color_palette, dtype=np.uint8)[:, ::-1] # RGB to BGR
        color_map = np.zeros((256, 1, 3), dtype=np.uint8)
        classes_num = len(classes)
        color_map[:classes_num, 0, :] = classes
        color_map[classes_num:, 0, :] = np.random.uniform(0, 255, size=(256-classes_num, 3))
        return color_map

    def apply_color_map(self, input):
        input_3d = cv2.merge([input, input, input])
        return cv2.LUT(input_3d, self.color_map)

    def overlay_masks(self, frame, objects, output_transform):
        # Visualizing result data over source image
        return output_transform.resize(np.floor_divide(frame, 2) + np.floor_divide(self.apply_color_map(objects), 2))


class SaliencyMapVisualizer:
    def overlay_masks(self, frame, objects, output_transform):
        saliency_map = (objects * 255).astype(np.uint8)
        saliency_map = cv2.merge([saliency_map, saliency_map, saliency_map])
        return output_transform.resize(np.floor_divide(frame, 2) + np.floor_divide(saliency_map, 2))

def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument('-m', '--model', help='Required. Path to an .xml file with a trained model.',
                      required=True, type=Path)
    args.add_argument('-at', '--architecture_type', help='Required. Specify the model\'s architecture type.',
                      type=str, required=True, choices=('segmentation', 'salient_object_detection'))
    args.add_argument('-i', '--input', required=True,
                      help='Required. An input to process. The input must be a single image, '
                           'a folder of images, video file or camera id.')
    args.add_argument('-d', '--device', default='CPU', type=str,
                      help='Optional. Specify the target device to infer on; CPU, GPU, HDDL or MYRIAD is '
                           'acceptable. The demo will look for a suitable plugin for device specified. '
                           'Default value is CPU.')

    common_model_args = parser.add_argument_group('Common model options')
    common_model_args.add_argument('-c', '--colors', type=Path,
                                   help='Optional. Path to a text file containing colors for classes.')
    common_model_args.add_argument('--labels', help='Optional. Labels mapping file.', default=None, type=str)

    infer_args = parser.add_argument_group('Inference options')
    infer_args.add_argument('-nireq', '--num_infer_requests', help='Optional. Number of infer requests.',
                            default=1, type=int)
    infer_args.add_argument('-nstreams', '--num_streams',
                            help='Optional. Number of streams to use for inference on the CPU or/and GPU in throughput '
                                 'mode (for HETERO and MULTI device cases use format '
                                 '<device1>:<nstreams1>,<device2>:<nstreams2> or just <nstreams>).',
                            default='', type=str)
    infer_args.add_argument('-nthreads', '--num_threads', default=None, type=int,
                            help='Optional. Number of threads to use for inference on CPU (including HETERO cases).')

    io_args = parser.add_argument_group('Input/output options')
    io_args.add_argument('--loop', default=False, action='store_true',
                         help='Optional. Enable reading the input in a loop.')
    io_args.add_argument('-o', '--output', required=False,
                         help='Optional. Name of the output file(s) to save.')
    io_args.add_argument('-limit', '--output_limit', required=False, default=1000, type=int,
                         help='Optional. Number of frames to store in output. '
                              'If 0 is set, all frames are stored.')
    io_args.add_argument('--no_show', help="Optional. Don't show output.", action='store_true')
    io_args.add_argument('--output_resolution', default=None, type=resolution,
                         help='Optional. Specify the maximum output window resolution '
                              'in (width x height) format. Example: 1280x720. '
                              'Input frame size used by default.')
    io_args.add_argument('-u', '--utilization_monitors', default='', type=str,
                         help='Optional. List of monitors to show initially.')

    debug_args = parser.add_argument_group('Debug options')
    debug_args.add_argument('-r', '--raw_output_message', help='Optional. Output inference results as mask histogram.',
                            default=False, action='store_true')
    return parser


def get_model(ie, args):
    if args.architecture_type == 'segmentation':
        return SegmentationModel(ie, args.model, labels=args.labels), SegmentationVisualizer(args.colors)
    if args.architecture_type == 'salient_object_detection':
        return SalientObjectDetectionModel(ie, args.model, labels=args.labels), SaliencyMapVisualizer()


def print_raw_results(mask, frame_id, labels=None):
    log.debug(' ---------------- Frame # {} ---------------- '.format(frame_id))
    log.debug('     Class ID     | Pixels | Percentage ')
    max_classes = int(np.max(mask)) + 1 # We use +1 for only background case
    histogram = cv2.calcHist([np.expand_dims(mask, axis=-1)], [0], None, [max_classes], [0, max_classes])
    all = np.product(mask.shape)
    for id, val in enumerate(histogram[:, 0]):
        if val > 0:
            label = labels[id] if labels and len(labels) >= id else '#{}'.format(id)
            log.debug(' {:<16} | {:6d} | {:5.2f}% '.format(label, int(val), val / all * 100))


def main():
    args = build_argparser().parse_args()

    cap = open_images_capture(args.input, args.loop)

    log.info('OpenVINO Inference Engine')
    log.info('\tbuild: {}'.format(get_version()))
    ie = IECore()

    plugin_config = get_user_config(args.device, args.num_streams, args.num_threads)

    model, visualizer = get_model(ie, args)
    log.info('Reading model {}'.format(args.model))
    log_blobs_info(model)

    pipeline = AsyncPipeline(ie, model, plugin_config, device=args.device, max_num_requests=args.num_infer_requests)

    log.info('The model {} is loaded to {}'.format(args.model, args.device))
    log_runtime_settings(pipeline.exec_net, set(parse_devices(args.device)))

    next_frame_id = 0
    next_frame_id_to_show = 0

    metrics = PerformanceMetrics()
    render_metrics = PerformanceMetrics()
    presenter = None
    output_transform = None
    video_writer = cv2.VideoWriter()

    while True:
        if pipeline.is_ready():
            # Get new image/frame
            start_time = perf_counter()
            frame = cap.read()
            if frame is None:
                if next_frame_id == 0:
                    raise ValueError("Can't read an image from the input")
                break
            if next_frame_id == 0:
                output_transform = OutputTransform(frame.shape[:2], args.output_resolution)
                if args.output_resolution:
                    output_resolution = output_transform.new_resolution
                else:
                    output_resolution = (frame.shape[1], frame.shape[0])
                presenter = monitors.Presenter(args.utilization_monitors, 55,
                                               (round(output_resolution[0] / 4), round(output_resolution[1] / 8)))
                if args.output and not video_writer.open(args.output, cv2.VideoWriter_fourcc(*'MJPG'),
                                                         cap.fps(), output_resolution):
                    raise RuntimeError("Can't open video writer")
            # Submit for inference
            pipeline.submit_data(frame, next_frame_id, {'frame': frame, 'start_time': start_time})
            next_frame_id += 1
        else:
            # Wait for empty request
            pipeline.await_any()

        if pipeline.callback_exceptions:
            raise pipeline.callback_exceptions[0]
        # Process all completed requests
        results = pipeline.get_result(next_frame_id_to_show)
        if results:
            objects, frame_meta = results
            if args.raw_output_message:
                print_raw_results(objects, next_frame_id_to_show, model.labels)
            frame = frame_meta['frame']
            start_time = frame_meta['start_time']
            rendering_start_time = perf_counter()
            frame = visualizer.overlay_masks(frame, objects, output_transform)
            render_metrics.update(rendering_start_time)
            presenter.drawGraphs(frame)
            metrics.update(start_time, frame)

            if video_writer.isOpened() and (args.output_limit <= 0 or next_frame_id_to_show <= args.output_limit-1):
                video_writer.write(frame)
            next_frame_id_to_show += 1

            if not args.no_show:
                cv2.imshow('Segmentation Results', frame)
                key = cv2.waitKey(1)
                if key == 27 or key == 'q' or key == 'Q':
                    break
                presenter.handleKey(key)

    pipeline.await_all()
    # Process completed requests
    for next_frame_id_to_show in range(next_frame_id_to_show, next_frame_id):
        results = pipeline.get_result(next_frame_id_to_show)
        while results is None:
            results = pipeline.get_result(next_frame_id_to_show)
        objects, frame_meta = results
        if args.raw_output_message:
            print_raw_results(objects, next_frame_id_to_show, model.labels)
        frame = frame_meta['frame']
        start_time = frame_meta['start_time']

        rendering_start_time = perf_counter()
        frame = visualizer.overlay_masks(frame, objects, output_transform)
        render_metrics.update(rendering_start_time)
        presenter.drawGraphs(frame)
        metrics.update(start_time, frame)

        if video_writer.isOpened() and (args.output_limit <= 0 or next_frame_id_to_show <= args.output_limit-1):
            video_writer.write(frame)

        if not args.no_show:
            cv2.imshow('Segmentation Results', frame)
            key = cv2.waitKey(1)

    metrics.log_total()
    pipeline_metrics = {'Decoding': cap.reader_metrics.get_latency(),
                        'Preprocessing': pipeline.preprocess_metrics.get_latency(),
                        'Inference': pipeline.inference_metrics.get_latency(),
                        'Postprocessing': pipeline.postprocess_metrics.get_latency(),
                        'Rendering': render_metrics.get_latency()}
    log_latency_per_stage(pipeline_metrics)
    for rep in presenter.reportMeans():
        log.info(rep)


if __name__ == '__main__':
    sys.exit(main() or 0)
