from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any


class GiftRarity(IntEnum):
    COMMON = 1
    ENCHANTED = 2
    RARE = 3
    EPIC = 4
    LEGENDARY = 5


RARETY_DISPLAY_NAMES: dict[GiftRarity, str] = {
    GiftRarity.COMMON: "Mầm thường",
    GiftRarity.ENCHANTED: "Linh thảo",
    GiftRarity.RARE: "Hoa nghi lễ",
    GiftRarity.EPIC: "Hạt bí ẩn",
    GiftRarity.LEGENDARY: "Cấm hoa",
}


def get_rarity_display_name(rarity: int) -> str:
    try:
        rarity_enum = GiftRarity(rarity)
    except ValueError:
        return "Không rõ"
    return RARETY_DISPLAY_NAMES.get(rarity_enum, "Không rõ")


class GiftID(StrEnum):
    SEVERED_GRASS_SILENCE = "severed_grass_silence"
    VOW_LOTUS_SEAL = "vow_lotus_seal"
    AMARANTH_HEART_LAMP = "amaranth_heart_lamp"
    FROST_FLOWER_WHISPER = "frost_flower_whisper"
    DAWN_BELL_HERB = "dawn_bell_herb"
    OTHERWORLDLY_FLOWER = "otherworldly_flower"


GIFT_DISPLAY_NAMES: dict[GiftID, str] = {
    GiftID.SEVERED_GRASS_SILENCE: "Cỏ Lặng Cắt Ký Ức",
    GiftID.VOW_LOTUS_SEAL: "Sen Ấn Thệ Ước",
    GiftID.AMARANTH_HEART_LAMP: "Đèn Tim Màu Dền",
    GiftID.FROST_FLOWER_WHISPER: "Hoa Sương Thì Thầm",
    GiftID.DAWN_BELL_HERB: "Cỏ Chuông Bình Minh",
    GiftID.OTHERWORLDLY_FLOWER: "Hoa Dị Giới",
}


def get_display_name(gift_id: GiftID) -> str:
    return GIFT_DISPLAY_NAMES.get(gift_id, "Hoa Dị Giới")


@dataclass(frozen=True)
class Gift:
    id: GiftID
    description: str
    price: int
    effects: dict[str, Any]
    consumable: bool = True
    comment: str = ""


ALL_GIFTS: dict[GiftID, Gift] = {
    GiftID.SEVERED_GRASS_SILENCE: Gift(
        id=GiftID.SEVERED_GRASS_SILENCE,
        description="Sắc hoa phai tàn, rễ cỏ lìa đứt; ký ức không hề bị xóa, chỉ là chẳng còn ai có thể chỉ ra rằng nó từng tồn tại.",
        price=4721,
        effects={},
        consumable=True,
        comment="Xóa ký ức",
    ),
    GiftID.VOW_LOTUS_SEAL: Gift(
        id=GiftID.VOW_LOTUS_SEAL,
        description="Lấy sen làm lời thề, lòng tĩnh như nước; mong người bình an, không sợ gió sóng.",
        price=2473,
        effects={"duration": 7200, "passivation": 3.7},
        consumable=True,
        comment="Trong một khoảng thời gian, giảm mạnh biến động độ thân thiết",
    ),
    GiftID.AMARANTH_HEART_LAMP: Gift(
        id=GiftID.AMARANTH_HEART_LAMP,
        description="Ánh đèn chưa tắt, trái tim vẫn hướng về; sắc dền như ráng chiều, ấm áp miên man.",
        price=983,
        effects={"add_affection": 263, "duration": 1800},
        consumable=True,
        comment="Tạm thời tăng mạnh độ thân thiết",
    ),
    GiftID.FROST_FLOWER_WHISPER: Gift(
        id=GiftID.FROST_FLOWER_WHISPER,
        description="Hoa nở trong tĩnh lặng, lòng cũng không lời; nghe thấu vạn âm để nhìn rõ nhân tâm.",
        price=3701,
        effects={},
        consumable=True,
        comment="Xem ký ức hiện tại về bạn",
    ),
    GiftID.DAWN_BELL_HERB: Gift(
        id=GiftID.DAWN_BELL_HERB,
        description="Chuông sớm vừa ngân, màn đêm tan biến; lá cỏ phủ sương mà ánh bình minh vẫn không tắt.",
        price=4549,
        effects={"unblock": True, "immune_duration": 1800},
        consumable=True,
        comment="Gỡ trạng thái bị chặn và miễn nhiễm bị chặn trong một thời gian",
    ),
}

OTHERWORLDLY_FLOWER = Gift(
    id=GiftID.OTHERWORLDLY_FLOWER,
    description="Một đóa hoa vốn không nên tồn tại ở nơi này.",
    price=9973,
    effects={},
    consumable=True,
    comment="Quà bất thường, đáng lẽ không nên xuất hiện",
)


def get_gift_by_id(gift_id: GiftID) -> Gift:
    gift = ALL_GIFTS.get(gift_id)
    if gift is None:
        return OTHERWORLDLY_FLOWER
    return gift


def list_all_gifts() -> list[Gift]:
    return list(ALL_GIFTS.values())


def list_affordable_gifts(coins: int) -> list[Gift]:
    return [gift for gift in ALL_GIFTS.values() if gift.price <= coins]
