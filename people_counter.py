import os
import sys

# MUST BE BEFORE IMPORTS: Silences the constant YOLO terminal spam
os.environ["YOLO_VERBOSE"] = "False"

import cv2
import time
import argparse
from datetime import datetime
from typing import Callable
from ultralytics import solutions


def run_monitor_loop(
    source: int | str,
    line_y: int,
    visual: bool,
    on_entry_callback: Callable[[int], None],
    on_frame_callback: Callable | None = None,
):
    """
    Core YOLO monitoring loop — importable by master-script.py.

    Parameters
    ----------
    source            : camera index (int) or stream URL (str)
    line_y            : Y-coordinate of the entry line
    visual            : whether to show the OpenCV window
    on_entry_callback : called with (people_in_frame: int) on each crossing
    on_frame_callback : optional, called with (frame) on every captured frame
    """
    region_points = [(100, 200), (500, 200), (500, 400), (100, 400)]

    trackzone = solutions.TrackZone(
        model="yolo11n.pt",
        region=region_points,
        show=visual,
        conf=0.5,
        classes=[0],
    )

    cap = cv2.VideoCapture(source)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print(f"❌ Error: Could not connect to camera source: {source}")
        return None   # caller decides whether to exit or retry

    entry_count     = 0
    tracked_entries = set()
    prev_centers    = {}

    print(f"✅ YOLO monitor started (source={source}, line_y={line_y}, visual={visual})")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            if on_frame_callback is not None:
                on_frame_callback(frame)

            results = trackzone(frame)

            if results is not None and hasattr(results, "plot_im"):
                annotated = results.plot_im
            elif results is not None and isinstance(results, list) and len(results) > 0:
                annotated = results[0].plot()
            else:
                annotated = frame

            if trackzone.track_ids is not None:
                people_in_frame = len(trackzone.track_ids)

                for i, track_id in enumerate(trackzone.track_ids):
                    if i < len(trackzone.boxes):
                        box = trackzone.boxes[i]
                        cx  = int((box[0] + box[2]) / 2)
                        cy  = int((box[1] + box[3]) / 2)

                        prev_cy = prev_centers.get(track_id)

                        if prev_cy is not None and track_id not in tracked_entries:
                            if prev_cy < line_y <= cy and 100 <= cx <= 500:
                                entry_count += 1
                                tracked_entries.add(track_id)
                                timestamp = datetime.now().strftime("%H:%M:%S")
                                print(f"🚪 Crossing #{entry_count} (ID: {track_id}) [{timestamp}]")
                                on_entry_callback(people_in_frame)

                        prev_centers[track_id] = cy

            if visual:
                cv2.line(annotated, (100, line_y), (500, line_y), (0, 255, 0), 3)
                cv2.imshow("Door Monitor", annotated)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            time.sleep(0.01)

    except KeyboardInterrupt:
        print(f"\n🛑 Stopped. Total entries this session: {entry_count}")
    finally:
        cap.release()
        if visual:
            cv2.destroyAllWindows()


def main():
    """Standalone entry point — unchanged behaviour from original."""
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Door Monitor")
    parser.add_argument("-s", "--source", default="0",
                        help="Camera source: 0 for webcam, or http://IP:81/stream")
    parser.add_argument("-l", "--line-y", type=int, default=300,
                        help="Y-coordinate for the green entry line")
    parser.add_argument("-v", "--visual", action="store_true",
                        help="Enable visual window for testing")
    args = parser.parse_args()

    source = int(args.source) if args.source.isdigit() else args.source

    def _print_entry(people_in_frame: int):
        pass  # standalone mode: crossing already printed inside run_monitor_loop

    run_monitor_loop(
        source=source,
        line_y=args.line_y,
        visual=args.visual,
        on_entry_callback=_print_entry,
    )


if __name__ == "__main__":
    main()
