import os
import sys

# MUST BE BEFORE IMPORTS: Silences the constant YOLO terminal spam
os.environ["YOLO_VERBOSE"] = "False"

import cv2
import time
import argparse
from datetime import datetime
from ultralytics import solutions

def main():
    # Define CLI Arguments
    parser = argparse.ArgumentParser(description="Tinkerer's Lab Door Monitor")
    parser.add_argument("-s", "--source", default="0", help="Camera source: 0 for webcam, or http://IP:81/stream")
    parser.add_argument("-l", "--line-y", type=int, default=300, help="Y-coordinate for the green entry line")
    parser.add_argument("-v", "--visual", action="store_true", help="Enable visual window for testing")
    args = parser.parse_args()

    # Convert source to integer if it's "0" (webcam)
    source = int(args.source) if args.source.isdigit() else args.source
    line_y = args.line_y

    region_points = [(100, 200), (500, 200), (500, 400), (100, 400)]

    # Initialize TrackZone
    trackzone = solutions.TrackZone(
        model="yolo11n.pt",  # Fix #1: corrected model name
        region=region_points,
        show=args.visual,
        conf=0.5,
        classes=[0]
    )

    cap = cv2.VideoCapture(source)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print(f"❌ Error: Could not connect to camera source: {source}")
        sys.exit(1)

    entry_count = 0
    tracked_entries = set()
    prev_centers = {}

    print(f"✅ Monitoring started!")
    print(f"Source: {source} | Line Y: {line_y} | Visual Mode: {'ON' if args.visual else 'OFF'}")
    print("Waiting for entries... (Press Ctrl+C to stop)\n")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.1)
                continue

            results = trackzone(frame)
            
            if results is not None and hasattr(results, 'plot_im'):
                annotated = results.plot_im
            elif results is not None and isinstance(results, list) and len(results) > 0:
                annotated = results[0].plot()
            else:
                annotated = frame
            
            if trackzone.track_ids is not None:
                for i, track_id in enumerate(trackzone.track_ids):
                    if i < len(trackzone.boxes):
                        box = trackzone.boxes[i]
                        cx = int((box[0] + box[2]) / 2)
                        cy = int((box[1] + box[3]) / 2)

                        prev_cy = prev_centers.get(track_id)  # Fix #4: None default, skip check on first observation

                        if prev_cy is not None and track_id not in tracked_entries:
                            # Fix #2: correctly triggers when moving downward (cy increasing past line_y)
                            if prev_cy < line_y <= cy and 100 <= cx <= 500:
                                entry_count += 1
                                tracked_entries.add(track_id)
                                timestamp = datetime.now().strftime("%H:%M:%S")
                                print(f"🚪 ENTRY #{entry_count} (ID: {track_id}) [{timestamp}]")

                        prev_centers[track_id] = cy

            if args.visual:
                cv2.line(annotated, (100, line_y), (500, line_y), (0, 255, 0), 3)  # Fix #3: draw on annotated
                cv2.imshow("Door Monitor Calibration", annotated)

                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            time.sleep(0.01)

    except KeyboardInterrupt:
        print(f"\n🛑 Stopped. Total entries today: {entry_count}")
    finally:
        cap.release()
        if args.visual:
            cv2.destroyAllWindows()

if __name__ == "__main__":
    main()