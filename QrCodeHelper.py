import qrcode as QrCodeHelper
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
from qrcode.image.styles.colormasks import SolidFillColorMask
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from PIL import Image, ImageDraw, ImageFilter
import os
import base64
from datetime import datetime, timedelta
import io

# ── Brand palette (pulled from favicon) ──────────────────────────────────────
CRIMSON   = (133, 22,  15)   # favicon red
BLACK     = (0,  0, 0)   # favicon black
OFF_WHITE = (245, 240, 238)  # warm white for modules against dark bg
DARK_BG   = (14, 10, 10)  # near-black with faint red warmth
# ─────────────────────────────────────────────────────────────────────────────

class AUColorMask(SolidFillColorMask):
    """
    Dark-theme color mask:
      - Background  -> deep near-black  (DARK_BG)
      - QR modules  -> off-white        (OFF_WHITE)  <- high contrast on dark
      - Finder eyes -> crimson          (CRIMSON)    <- brand accent
    """
    def __init__(self):
        super().__init__(front_color=OFF_WHITE, back_color=DARK_BG)
        self.eye_color = CRIMSON

    def initialize(self, styled_image, img):
        super().initialize(styled_image, img)
        self._box_size = styled_image.box_size
        self._border   = styled_image.border
        self._img_size = img.size

    def apply_mask(self, image):
        super().apply_mask(image)

        draw = ImageDraw.Draw(image)
        box_size = self._box_size
        border = self._border * box_size
        eye_size = 7 * box_size
        w, h = self._img_size

        for x0, y0, x1, y1 in [
            (border,              border,              border + eye_size,     border + eye_size),
            (w-border-eye_size,   border,              w-border,              border + eye_size),
            (border,              h-border-eye_size,   border + eye_size,     h-border),
        ]:
            draw.rectangle([x0, y0, x1, y1], fill=self.eye_color)


def _make_rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([(0, 0), (size[0]-1, size[1]-1)], radius=radius, fill=255)
    return mask


def _composite_logo(qr_img, logo_path):
    qr_w, qr_h = qr_img.size

    logo_area = int(min(qr_w, qr_h) * 0.22)
    frame_pad = int(logo_area * 0.15)
    quiet_pad = int(logo_area * 0.08)
    logo_size = logo_area
    frame_size = logo_size + frame_pad * 2
    total_size = frame_size + quiet_pad * 2

    logo = Image.open(logo_path).convert("RGBA").resize(
        (logo_size, logo_size), Image.LANCZOS
    )

    quiet = Image.new("RGBA", (total_size, total_size), (*DARK_BG, 255))
    quiet_mask = _make_rounded_mask((total_size, total_size), radius=int(total_size * 0.18))
    quiet.putalpha(quiet_mask)

    frame = Image.new("RGBA", (frame_size, frame_size), (*CRIMSON, 255))
    frame_mask = _make_rounded_mask((frame_size, frame_size), radius=int(frame_size * 0.20))
    frame.putalpha(frame_mask)

    inner_pad  = int(frame_pad * 0.35)
    inner_size = logo_size + inner_pad * 2
    inner      = Image.new("RGBA", (inner_size, inner_size), (*DARK_BG, 255))
    inner_mask = _make_rounded_mask((inner_size, inner_size), radius=int(inner_size * 0.18))
    inner.putalpha(inner_mask)

    composite = Image.new("RGBA", (total_size, total_size), (0, 0, 0, 0))

    def centre_paste(base, layer):
        bw, bh = base.size
        lw, lh = layer.size
        base.paste(layer, ((bw - lw) // 2, (bh - lh) // 2), layer)

    centre_paste(composite, quiet)
    centre_paste(composite, frame)
    centre_paste(composite, inner)
    centre_paste(composite, logo)

    qr_rgba = qr_img.convert("RGBA")
    ox = (qr_w - total_size) // 2
    oy = (qr_h - total_size) // 2
    qr_rgba.paste(composite, (ox, oy), composite)
    return qr_rgba.convert("RGB")

def _finalize_qr(raw_qr_img, logo_path):
    if logo_path and os.path.isfile(logo_path):
        qr = _composite_logo(raw_qr_img, logo_path)
    else:
        qr = raw_qr_img.convert("RGB")

    pad = 40
    out_w = qr.width + pad * 2
    out_h = qr.height + pad * 2
    canvas = Image.new("RGB", (out_w, out_h), DARK_BG)
    canvas.paste(qr, (pad, pad))

    d = ImageDraw.Draw(canvas)
    accent_w = 4
    accent_l = 48
    r = 12
    corners = [
        [(r, 0),              (accent_l, 0)],
        [(0, r),              (0, accent_l)],
        [(out_w-accent_l, 0), (out_w-r, 0)],
        [(out_w-1, r),        (out_w-1, accent_l)],
        [(0, out_h-accent_l), (0, out_h-r)],
        [(r, out_h-1),        (accent_l, out_h-1)],
        [(out_w-accent_l, out_h-1), (out_w-r, out_h-1)],
        [(out_w-1, out_h-accent_l), (out_w-1, out_h-r)],
    ]
    for line in corners:
        d.line(line, fill=CRIMSON, width=accent_w)
    return canvas

class LabAccessQrCode:
    _PRIV_KEY = b'j:\xbff\xcdV\x80\x7fP\xad\xefh\xdcZ\x8e\x97\xebfJH\xf2\xe7\x948\x02\xfd\xbfd\x8a\xad\xf0F'
    def __init__(self, expiration_minutes=60, logo_path=None):
        self.expiration = timedelta(minutes=expiration_minutes)
        self.logo_path  = logo_path if (logo_path and os.path.isfile(logo_path)) else None
    def generate(self, name, enrolment_id):
        if not name or not enrolment_id:
            raise ValueError("Name and Enrolment ID must be non-empty strings")
        current_time = datetime.now().isoformat()
        text_to_encrypt = f"{name}_{enrolment_id}_{current_time}"
        aes = AESGCM(self._PRIV_KEY)
        nonce = os.urandom(12)
        ciphertext = aes.encrypt(nonce, text_to_encrypt.encode('utf-8'), None)
        payload = nonce + ciphertext
        encoded_payload = base64.urlsafe_b64encode(payload).decode('utf-8')
        qr = QrCodeHelper.QRCode(
            error_correction=QrCodeHelper.constants.ERROR_CORRECT_H,
            box_size=12,
            border=2,
        )
        qr.add_data(encoded_payload)
        qr.make(fit=True)
        raw_img = qr.make_image(
            image_factory=StyledPilImage,
            module_drawer=RoundedModuleDrawer(radius_ratio=0.8),
            color_mask=AUColorMask(),
        )
        final_img = _finalize_qr(raw_img, self.logo_path)
        buffer = io.BytesIO()
        final_img.save(buffer, format="PNG")
        buffer.seek(0)
        return encoded_payload, buffer

    def validate(self, scanned_data):
        try:
            payload = base64.urlsafe_b64decode(scanned_data)
            nonce = payload[:12]
            ciphertext = payload[12:]

            aes = AESGCM(self._PRIV_KEY)
            decrypted_bytes = aes.decrypt(nonce, ciphertext, None)
            decrypted_text = decrypted_bytes.decode('utf-8')

            parts = decrypted_text.rsplit('_', 2)
            if len(parts) != 3:
                return False, "Invalid QR Code format."

            name, enrolment_id, timestamp = parts
            creationTime = datetime.fromisoformat(timestamp)
            time_gone = datetime.now() - creationTime

            if time_gone > self.expiration:
                return False, f"Access Denied: QR Code expired {time_gone} ago."

            return True, f"Access Granted for {name} (ID: {enrolment_id})."
        except InvalidTag:
            return False, "Access Denied: QR Code is invalid or has been tampered with."
        except Exception as e:
            return False, f"Validation Error: {str(e)}"

if __name__ == "__main__": #ignore this part, added incase we forget to remove it, so that the testing doesn't interfere with module as such

    lab_system = LabAccessQrCode(expiration_minutes=5, logo_path="favicon.png") #for logo thing, in case we got tl logo
    scanned_string, image_buffer = lab_system.generate(name="Vansh Shah", enrolment_id="AU2540082") 
    print(f"Payload String: {scanned_string[:30]}...") # preview to look cool, this is what we send to whatsapp (buffer), so we dont waste time for saving output and then send!
    print(f"Buffer Size: {image_buffer.getbuffer().nbytes} bytes") #size to check if exists or not (by chance)
    with open("output.png", "wb") as f:
        f.write(image_buffer.read()) #saving as output
    print("Test passed.")
    print()
    is_valid, message = lab_system.validate(scanned_string)
    print(f"Valid: {is_valid} || Message: {message}") # valid will be true
    print()
    tampered_string = scanned_string[:-5] + "adsonefwcs"
    is_valid, message = lab_system.validate(tampered_string)
    print(f"Valid: {is_valid} || Message: {message}") # false, cuz random shit