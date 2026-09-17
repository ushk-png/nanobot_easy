#!/usr/bin/env python3
"""Prepare Frank's daily Japanese sentence study message and audio input.

This script keeps the learning/review schedule deterministic so the daily cron
agent does not have to improvise JSON updates. It creates the configured number
of new sentences for the target date when they do not already exist, selects
reviews strictly from the stored next_review_date, writes the Telegram message
text, and advances only the reviews that are actually included in that message.
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import agent_reliability as reliability

DEFAULT_DATA_PATH = Path("memory/japanese_sentence_learning.json")
DEFAULT_OUT_DIR = Path("out/audio")
DEFAULT_VOCAB_REQUESTS_PATH = Path("memory/daily_sentence_vocab_requests.json")
DEFAULT_RELIABILITY_DB_PATH = Path("memory/agent_reliability.sqlite3")

NEW_SENTENCE_BANK: dict[str, list[tuple[str, str, str]]] = {
    "2026-08-08": [
        ("注文をお願いします。", "추-몬오 오네가이시마스.", "주문 부탁드립니다."),
        ("これは何の料理ですか。", "코레와 난노 료-리데스카.", "이것은 무슨 요리입니까?"),
        ("水をもう一杯ください。", "미즈오 모- 잇파이 쿠다사이.", "물 한 잔 더 주세요."),
        ("会計をお願いします。", "카이케-오 오네가이시마스.", "계산 부탁드립니다."),
        ("とてもおいしかったです。", "토테모 오이시캇타데스.", "정말 맛있었습니다."),
    ],
    "2026-08-15": [
        ("空港までお願いします。", "쿠-코-마데 오네가이시마스.", "공항까지 부탁드립니다."),
        ("このホテルに泊まります。", "코노 호테루니 토마리마스.", "이 호텔에 묵습니다."),
        ("予約を確認してください。", "요야쿠오 카쿠닌시테 쿠다사이.", "예약을 확인해 주세요."),
        ("コインロッカーを使いたいです。", "코인 롯카-오 츠카이타이데스.", "코인 로커를 사용하고 싶습니다."),
        ("駅の近くで降ります。", "에키노 치카쿠데 오리마스.", "역 근처에서 내립니다."),
        ("この店で払えますか。", "코노 미세데 하라에마스카.", "이 가게에서 결제할 수 있습니까?"),
        ("バス停はどこにありますか。", "바스테-와 도코니 아리마스카.", "버스 정류장은 어디에 있습니까?"),
    ],
    "2026-08-16": [
        ("充電器を借りられますか。", "주-덴키오 카리라레마스카.", "충전기를 빌릴 수 있습니까?"),
        ("タオルをもう一枚ください。", "타오루오 모- 이치마이 쿠다사이.", "수건을 한 장 더 주세요."),
        ("部屋が少し寒いです。", "헤야가 스코시 사무이데스.", "방이 조금 춥습니다."),
        ("エアコンを止めてもいいですか。", "에아콘오 토메테모 이이데스카.", "에어컨을 꺼도 될까요?"),
        ("朝、タクシーを呼んでください。", "아사, 타쿠시-오 욘데 쿠다사이.", "아침에 택시를 불러 주세요."),
        ("近くの薬局を教えてください。", "치카쿠노 야쿄쿠오 오시에테 쿠다사이.", "근처 약국을 알려 주세요."),
        ("このカードで払います。", "코노 카-도데 하라이마스.", "이 카드로 결제하겠습니다."),
    ],
    "2026-08-17": [
        ("道に迷いました。", "미치니 마요이마시타.", "길을 잃었습니다."),
        ("この住所まで行きたいです。", "코노 주-쇼마데 이키타이데스.", "이 주소까지 가고 싶습니다."),
        ("日本語が少しわかります。", "니혼고가 스코시 와카리마스.", "일본어를 조금 압니다."),
        ("もう一度言ってください。", "모- 이치도 잇테 쿠다사이.", "한 번 더 말해 주세요."),
        ("現金で払えますか。", "겐킨데 하라에마스카.", "현금으로 결제할 수 있습니까?"),
        ("袋を一つください。", "후쿠로오 히토츠 쿠다사이.", "봉투 하나 주세요."),
        ("おつりを確認します。", "오츠리오 카쿠닌시마스.", "거스름돈을 확인하겠습니다."),
    ],
    "2026-08-18": [
        ("入口でチケットを見せます。", "이리구치데 치켓토오 미세마스.", "입구에서 티켓을 보여 줍니다."),
        ("案内所で聞いてみます。", "안나이조데 키이테 미마스.", "안내소에서 물어보겠습니다."),
        ("この電車は何番線ですか。", "코노 덴샤와 난반센데스카.", "이 전철은 몇 번 승강장입니까?"),
        ("荷物置き場を使えますか。", "니모츠오키바오 츠카에마스카.", "짐 보관 공간을 사용할 수 있습니까?"),
        ("駅員さんに聞きます。", "에키인산니 키키마스.", "역무원에게 물어보겠습니다."),
        ("ホテルに戻りたいです。", "호테루니 모도리타이데스.", "호텔로 돌아가고 싶습니다."),
        ("少し安くなりますか。", "스코시 야스쿠 나리마스카.", "조금 싸게 되나요?"),
    ],
    "2026-08-19": [
        ("搭乗手続きをします。", "토-죠- 테츠즈키오 시마스.", "탑승 수속을 하겠습니다."),
        ("窓側の席がいいです。", "마도와노 세키가 이이데스.", "창가 자리가 좋습니다."),
        ("出発は何時ですか。", "슛파츠와 난지데스카.", "출발은 몇 시입니까?"),
        ("乗り換えは必要ですか。", "노리카에와 히츠요-데스카.", "환승이 필요합니까?"),
        ("改札はどこですか。", "카이사츠와 도코데스카.", "개찰구는 어디입니까?"),
        ("ここに並べばいいですか。", "코코니 나라베바 이이데스카.", "여기에 줄 서면 됩니까?"),
        ("写真を一枚撮ります。", "샤신오 이치마이 토리마스.", "사진을 한 장 찍겠습니다."),
    ],
    "2026-08-20": [
        ("スーツケースを預けます。", "스-츠케-스오 아즈케마스.", "여행 가방을 맡깁니다."),
        ("搭乗券を見せます。", "토-죠-켄오 미세마스.", "탑승권을 보여 줍니다."),
        ("保安検査はどこですか。", "호안켄사와 도코데스카.", "보안 검사는 어디입니까?"),
        ("水を買ってから行きます。", "미즈오 캇테카라 이키마스.", "물을 사고 나서 가겠습니다."),
        ("席を替えられますか。", "세키오 카에라레마스카.", "자리를 바꿀 수 있습니까?"),
        ("到着ロビーで会います。", "토-챠쿠 로비-데 아이마스.", "도착 로비에서 만납니다."),
        ("出口で待っています。", "데구치데 맛테이마스.", "출구에서 기다리고 있습니다."),
    ],
    "2026-08-21": [
        ("入国カードを書きます。", "뉴-코쿠 카-도오 카키마스.", "입국 카드를 씁니다."),
        ("税関で申告します。", "제-칸데 신고쿠시마스.", "세관에서 신고합니다."),
        ("案内板を確認します。", "안나이반오 카쿠닌시마스.", "안내판을 확인합니다."),
        ("駅の窓口で聞きます。", "에키노 마도구치데 키키마스.", "역 창구에서 물어봅니다."),
        ("地下鉄に乗ります。", "치카테츠니 노리마스.", "지하철을 탑니다."),
        ("お店の前で待ちます。", "오미세노 마에데 마치마스.", "가게 앞에서 기다립니다."),
        ("番号札を取ります。", "방고-후다오 토리마스.", "번호표를 뽑습니다."),
    ],
    "2026-08-22": [
        ("手荷物を受け取ります。", "테니모츠오 우케토리마스.", "휴대 수하물을 받습니다."),
        ("空港の出口で会いましょう。", "쿠-코-노 데구치데 아이마쇼-.", "공항 출구에서 만납시다."),
        ("リムジンバスに乗ります。", "리무진 바스니 노리마스.", "리무진 버스를 탑니다."),
        ("ホテルの住所を見せます。", "호테루노 주-쇼오 미세마스.", "호텔 주소를 보여 줍니다."),
        ("フロントで地図をもらいます。", "후론토데 치즈오 모라이마스.", "프런트에서 지도를 받습니다."),
        ("荷物だけ預けてもいいですか。", "니모츠다케 아즈케테모 이이데스카.", "짐만 맡겨도 될까요?"),
        ("近くで昼ご飯を食べます。", "치카쿠데 히루고한오 타베마스.", "근처에서 점심을 먹습니다."),
    ],
    "2026-08-23": [
        ("駅前のカフェで休みます。", "에키마에노 카페데 야스미마스.", "역 앞 카페에서 쉽니다."),
        ("朝ご飯は外で食べます。", "아사고한와 소토데 타베마스.", "아침밥은 밖에서 먹습니다."),
        ("バスの時間を確認します。", "바스노 지칸오 카쿠닌시마스.", "버스 시간을 확인합니다."),
        ("観光パンフレットをもらいます。", "칸코- 판후렛토오 모라이마스.", "관광 팸플릿을 받습니다."),
        ("この通りを渡ります。", "코노 토오리오 와타리마스.", "이 길을 건넙니다."),
        ("飲み物を一つ買います。", "노미모노오 히토츠 카이마스.", "마실 것을 하나 삽니다."),
        ("夕方まで散歩します。", "유-가타마데 산포시마스.", "저녁 무렵까지 산책합니다."),
    ],
    "2026-08-24": [
        ("券売機で切符を買います。", "켄바이키데 킷푸오 카이마스.", "발권기에서 표를 삽니다."),
        ("大きい荷物があります。", "오-키이 니모츠가 아리마스.", "큰 짐이 있습니다."),
        ("駅の出口で待ちます。", "에키노 데구치데 마치마스.", "역 출구에서 기다립니다."),
        ("次のバスに乗ります。", "츠기노 바스니 노리마스.", "다음 버스를 탑니다."),
        ("店員さんを呼びます。", "텐인산오 요비마스.", "점원을 부릅니다."),
        ("メニューをゆっくり見ます。", "메뉴-오 윳쿠리 미마스.", "메뉴를 천천히 봅니다."),
        ("お土産を家に送ります。", "오미야게오 이에니 오쿠리마스.", "기념품을 집으로 보냅니다."),
    ],
    "2026-08-25": [
        ("駅のホームで待ちます。", "에키노 호-무데 마치마스.", "역 승강장에서 기다립니다."),
        ("電車の時間を調べます。", "덴샤노 지칸오 시라베마스.", "전철 시간을 알아봅니다."),
        ("ホテルのロビーにいます。", "호테루노 로비-니 이마스.", "호텔 로비에 있습니다."),
        ("朝の便で帰ります。", "아사노 빈데 카에리마스.", "아침 항공편으로 돌아갑니다."),
        ("お茶を一つ頼みます。", "오차오 히토츠 타노미마스.", "차를 하나 주문합니다."),
        ("この靴を見せてください。", "코노 쿠츠오 미세테 쿠다사이.", "이 신발을 보여 주세요."),
        ("観光案内を読みます。", "칸코- 안나이오 요미마스.", "관광 안내를 읽습니다."),
    ],
    "2026-08-26": [
        ("空港の案内所に行きます。", "쿠-코-노 안나이조니 이키마스.", "공항 안내소에 갑니다."),
        ("乗車券をなくしました。", "죠-샤켄오 나쿠시마시타.", "승차권을 잃어버렸습니다."),
        ("このホテルを探しています。", "코노 호테루오 사가시테이마스.", "이 호텔을 찾고 있습니다."),
        ("夕食は何時までですか。", "유-쇼쿠와 난지마데데스카.", "저녁 식사는 몇 시까지입니까?"),
        ("この料理は辛いですか。", "코노 료-리와 카라이데스카.", "이 요리는 맵습니까?"),
        ("支払いは別々にできますか。", "시하라이와 베츠베츠니 데키마스카.", "계산을 따로 할 수 있습니까?"),
        ("お土産売り場は二階です。", "오미야게 우리바와 니카이데스.", "기념품 매장은 2층입니다."),
    ],
    "2026-08-27": [
        ("フロントで鍵を受け取ります。", "후론토데 카기오 우케토리마스.", "프런트에서 열쇠를 받습니다."),
        ("部屋に荷物を置きます。", "헤야니 니모츠오 오키마스.", "방에 짐을 둡니다."),
        ("駅まで歩いて行けますか。", "에키마데 아루이테 이케마스카.", "역까지 걸어서 갈 수 있습니까?"),
        ("この店は予約できますか。", "코노 미세와 요야쿠 데키마스카.", "이 가게는 예약할 수 있습니까?"),
        ("おすすめの場所を教えてください。", "오스스메노 바쇼오 오시에테 쿠다사이.", "추천 장소를 알려 주세요."),
        ("電車を一本待ちます。", "덴샤오 잇폰 마치마스.", "전철을 한 대 기다립니다."),
        ("お水は無料ですか。", "오미즈와 무료-데스카.", "물은 무료입니까?"),
    ],
    "2026-08-28": [
        ("レンタカーを予約しました。", "렌타카-오 요야쿠시마시타.", "렌터카를 예약했습니다."),
        ("ガソリンを満タンにしてください。", "가소린오 만탄니 시테 쿠다사이.", "휘발유를 가득 채워 주세요."),
        ("高速道路を使いますか。", "코-소쿠도-로오 츠카이마스카.", "고속도로를 이용합니까?"),
        ("駐車場は近くにありますか。", "추-샤조-와 치카쿠니 아리마스카.", "주차장은 근처에 있습니까?"),
        ("雨が降りそうです。", "아메가 후리소-데스.", "비가 올 것 같습니다."),
        ("傘を買いたいです。", "카사오 카이타이데스.", "우산을 사고 싶습니다."),
        ("薬を飲んでもいいですか。", "쿠스리오 논데모 이이데스카.", "약을 먹어도 될까요?"),
    ],
    "2026-08-31": [
        ("駅の改札で待ち合わせます。", "에키노 카이사츠데 마치아와세마스.", "역 개찰구에서 만나기로 합니다."),
        ("ホテルの朝食券を持っています。", "호테루노 초-쇼쿠켄오 못테이마스.", "호텔 조식권을 가지고 있습니다."),
        ("空港でSIMカードを買います。", "쿠-코-데 심 카-도오 카이마스.", "공항에서 SIM 카드를 삽니다."),
        ("このレストランは混んでいますか。", "코노 레스토랑와 콘데이마스카.", "이 레스토랑은 붐비고 있습니까?"),
        ("電車の遅れを確認します。", "덴샤노 오쿠레오 카쿠닌시마스.", "전철 지연을 확인합니다."),
        ("二番出口から出ます。", "니반 데구치카라 데마스.", "2번 출구로 나갑니다."),
        ("お会計はテーブルでできますか。", "오카이케-와 테-부루데 데키마스카.", "계산은 테이블에서 할 수 있습니까?"),
    ],
    "2026-09-02": [
        ("空港のカウンターで聞いてみます。", "쿠-코-노 카운타-데 키이테 미마스.", "공항 카운터에서 물어보겠습니다."),
        ("ホームの番号を確認します。", "호-무노 방고-오 카쿠닌시마스.", "승강장 번호를 확인합니다."),
        ("旅行の予定をもう一度見ます。", "료코-노 요테-오 모- 이치도 미마스.", "여행 일정을 한 번 더 봅니다."),
        ("入口の近くで友だちを待ちます。", "이리구치노 치카쿠데 토모다치오 마치마스.", "입구 근처에서 친구를 기다립니다."),
        ("この店でお茶だけ飲みます。", "코노 미세데 오차다케 노미마스.", "이 가게에서 차만 마십니다."),
        ("帰りの切符を先に買います。", "카에리노 킷푸오 사키니 카이마스.", "돌아가는 표를 먼저 삽니다."),
        ("ホテルの近くを少し歩きます。", "호테루노 치카쿠오 스코시 아루키마스.", "호텔 근처를 조금 걷습니다."),
    ],
    "2026-09-03": [
        ("駅のコインロッカーを探します。", "에키노 코인 롯카-오 사가시마스.", "역의 코인 로커를 찾습니다."),
        ("切符売り場はあちらです。", "킷푸 우리바와 아치라데스.", "매표소는 저쪽입니다."),
        ("このバス停で待ちます。", "코노 바스테-데 마치마스.", "이 버스 정류장에서 기다립니다."),
        ("ホテルまで歩きます。", "호테루마데 아루키마스.", "호텔까지 걸어갑니다."),
        ("朝ご飯の場所を聞きます。", "아사고한노 바쇼오 키키마스.", "아침 식사 장소를 물어봅니다."),
        ("レストランの入口で待ちます。", "레스토랑노 이리구치데 마치마스.", "레스토랑 입구에서 기다립니다."),
        ("お土産を友だちに買います。", "오미야게오 토모다치니 카이마스.", "기념품을 친구에게 삽니다."),
    ],
    "2026-09-04": [
        ("空港で両替します。", "쿠-코-데 료-가에시마스.", "공항에서 환전합니다."),
        ("バスの乗り場を探します。", "바스노 노리바오 사가시마스.", "버스 타는 곳을 찾습니다."),
        ("入口で案内を見ます。", "이리구치데 안나이오 미마스.", "입구에서 안내를 봅니다."),
        ("レストランで席を待ちます。", "레스토랑데 세키오 마치마스.", "레스토랑에서 자리를 기다립니다."),
        ("店でお土産を選びます。", "미세데 오미야게오 에라비마스.", "가게에서 기념품을 고릅니다."),
        ("この電車で渋谷へ行きます。", "코노 덴샤데 시부야에 이키마스.", "이 전철로 시부야에 갑니다."),
        ("フロントでタクシーを頼みます。", "후론토데 타쿠시-오 타노미마스.", "프런트에서 택시를 부탁합니다."),
    ],
    "2026-09-07": [
        ("空港でWi-Fiを使います。", "쿠-코-데 와이파이오 츠카이마스.", "공항에서 와이파이를 사용합니다."),
        ("ホテルの部屋番号を確認します。", "호테루노 헤야방고-오 카쿠닌시마스.", "호텔 방 번호를 확인합니다."),
        ("観光バスのチケットを買います。", "칸코- 바스노 치켓토오 카이마스.", "관광버스 티켓을 삽니다."),
        ("この道を右に曲がります。", "코노 미치오 미기니 마가리마스.", "이 길을 오른쪽으로 돕니다."),
        ("駅員さんに乗り場を聞きます。", "에키인산니 노리바오 키키마스.", "역무원에게 타는 곳을 물어봅니다."),
        ("空港の出口でタクシーを待ちます。", "쿠-코-노 데구치데 타쿠시-오 마치마스.", "공항 출구에서 택시를 기다립니다."),
        ("ショッピングモールで買い物します。", "숏핑구 모-루데 카이모노시마스.", "쇼핑몰에서 쇼핑합니다."),
    ],
    "2026-09-08": [
        ("駅の案内板を見ます。", "에키노 안나이반오 미마스.", "역의 안내판을 봅니다."),
        ("この電車は京都に行きますか。", "코노 덴샤와 쿄-토니 이키마스카.", "이 전철은 교토에 갑니까?"),
        ("宿泊カードに名前を書きます。", "슈쿠하쿠 카-도니 나마에오 카키마스.", "숙박 카드에 이름을 씁니다."),
        ("荷物をフロントに預けます。", "니모츠오 후론토니 아즈케마스.", "짐을 프런트에 맡깁니다."),
        ("近くのコンビニに行きます。", "치카쿠노 콘비니니 이키마스.", "근처 편의점에 갑니다."),
        ("この席で食べてもいいですか。", "코노 세키데 타베테모 이이데스카.", "이 자리에서 먹어도 될까요?"),
        ("買った物をかばんに入れます。", "캇타 모노오 카방니 이레마스.", "산 물건을 가방에 넣습니다."),
    ],
    "2026-09-14": [
        ("地下鉄の一日券をください。", "치카테츠노 이치니치켄오 쿠다사이.", "지하철 1일권을 주세요."),
        ("薬局は近くにありますか。", "야쿄쿠와 치카쿠니 아리마스카.", "약국은 근처에 있습니까?"),
        ("ここで休んでもいいですか。", "코코데 야슨데모 이이데스카.", "여기서 쉬어도 될까요?"),
        ("電池を買いたいです。", "덴치오 카이타이데스.", "건전지를 사고 싶습니다."),
        ("階段を上がります。", "카이단오 아가리마스.", "계단을 올라갑니다."),
        ("この店は何時までですか。", "코노 미세와 난지마데데스카.", "이 가게는 몇 시까지입니까?"),
        ("傘を一本ください。", "카사오 잇폰 쿠다사이.", "우산 하나 주세요."),
    ],
    "2026-09-15": [
        ("温泉までバスで行けますか。", "온센마데 바스데 이케마스카.", "온천까지 버스로 갈 수 있습니까?"),
        ("美術館は月曜日に休みです。", "비주츠칸와 게츠요-비니 야스미데스.", "미술관은 월요일에 쉽니다."),
        ("船のチケット売り場を探しています。", "후네노 치켓토 우리바오 사가시테이마스.", "배 티켓 매표소를 찾고 있습니다."),
        ("展望台から海を見ます。", "텐보-다이카라 우미오 미마스.", "전망대에서 바다를 봅니다."),
        ("お弁当を一つ買います。", "오벤토-오 히토츠 카이마스.", "도시락을 하나 삽니다."),
        ("道路の反対側に渡ります。", "도-로노 한타이가와니 와타리마스.", "도로 반대편으로 건너갑니다."),
        ("旅館に電話します。", "료칸니 덴와시마스.", "료칸에 전화합니다."),
    ],
    "2026-09-16": [
        ("駅ビルの本屋に寄ります。", "에키비루노 혼야니 요리마스.", "역 건물의 서점에 들릅니다."),
        ("お寺の門をくぐります。", "오테라노 몬오 쿠구리마스.", "절의 문을 지나갑니다."),
        ("地下街でお菓子を買います。", "치카가이데 오카시오 카이마스.", "지하상가에서 과자를 삽니다."),
        ("湖のそばを歩きます。", "미즈우미노 소바오 아루키마스.", "호수 옆을 걷습니다."),
        ("券の有効期限を見ます。", "켄노 유-코-키겐오 미마스.", "표의 유효기간을 봅니다."),
        ("連絡先を紙に書きます。", "렌라쿠사키오 카미니 카키마스.", "연락처를 종이에 씁니다."),
        ("北口のベンチに座ります。", "키타구치노 벤치니 스와리마스.", "북쪽 출구 벤치에 앉습니다."),
    ],
    "2026-09-17": [
        ("空港の売店で水を買います。", "쿠-코-노 바이텐데 미즈오 카이마스.", "공항 매점에서 물을 삽니다."),
        ("ホテルのエレベーターを使います。", "호테루노 에레베-타-오 츠카이마스.", "호텔 엘리베이터를 이용합니다."),
        ("港の近くで朝市を見ます。", "미나토노 치카쿠데 아사이치오 미마스.", "항구 근처에서 아침 시장을 봅니다."),
        ("交番で道を聞いてみます。", "코-반데 미치오 키이테 미마스.", "파출소에서 길을 물어보겠습니다."),
        ("レストランで窓側の席を頼みます。", "레스토랑데 마도가와노 세키오 타노미마스.", "레스토랑에서 창가 자리를 부탁합니다."),
        ("お土産店で箱入りのお菓子を選びます。", "오미야게텐데 하코이리노 오카시오 에라비마스.", "기념품 가게에서 상자에 든 과자를 고릅니다."),
        ("バスの運転手さんに行き先を聞きます。", "바스노 운텐슈산니 이키사키오 키키마스.", "버스 운전기사님에게 목적지를 물어봅니다."),
    ],
}

# Default new material must stay travel-focused.  Avoid generic daily-life
# sentences here because this list is used whenever a date-specific bank is not
# configured for the cron run.
TRAVEL_FALLBACK_SENTENCES: list[tuple[str, str, str]] = [
    ("パスポートを見せます。", "파스포-토오 미세마스.", "여권을 보여 줍니다."),
    ("搭乗口は何番ですか。", "토-죠-구치와 난반데스카.", "탑승구는 몇 번입니까?"),
    ("荷物はどこで受け取りますか。", "니모츠와 도코데 우케토리마스카.", "짐은 어디에서 받습니까?"),
    ("タクシー乗り場はあちらです。", "타쿠시- 노리바와 아치라데스.", "택시 승강장은 저쪽입니다."),
    ("この電車は新宿に行きますか。", "코노 덴샤와 신주쿠니 이키마스카.", "이 전철은 신주쿠에 갑니까?"),
    ("次の駅で降ります。", "츠기노 에키데 오리마스.", "다음 역에서 내립니다."),
    ("このお土産を見たいです。", "코노 오미야게오 미타이데스.", "이 기념품을 보고 싶습니다."),
    ("両替はどこでできますか。", "료-가에와 도코데 데키마스카.", "환전은 어디에서 할 수 있습니까?"),
    ("予約の名前はフランクです。", "요야쿠노 나마에와 프랑쿠데스.", "예약 이름은 프랭크입니다."),
    ("朝食は何時からですか。", "초-쇼쿠와 난지카라데스카.", "아침 식사는 몇 시부터입니까?"),
    ("部屋の鍵をください。", "헤야노 카기오 쿠다사이.", "방 열쇠를 주세요."),
    ("駅までタクシーで行きます。", "에키마데 타쿠시-데 이키마스.", "역까지 택시로 갑니다."),
    ("ここで写真を撮れますか。", "코코데 샤신오 토레마스카.", "여기에서 사진을 찍을 수 있습니까?"),
    ("入口はどちらですか。", "이리구치와 도치라데스카.", "입구는 어느 쪽입니까?"),
    ("出口は右です。", "데구치와 미기데스.", "출구는 오른쪽입니다."),
    ("このバスは空港へ行きますか。", "코노 바스와 쿠-코-에 이키마스카.", "이 버스는 공항에 갑니까?"),
    ("切符を二枚ください。", "킷푸오 니마이 쿠다사이.", "표 두 장 주세요."),
    ("この席は空いていますか。", "코노 세키와 아이테이마스카.", "이 자리는 비어 있습니까?"),
    ("おすすめは何ですか。", "오스스메와 난데스카.", "추천은 무엇입니까?"),
    ("辛くしないでください。", "카라쿠 시나이데 쿠다사이.", "맵게 하지 말아 주세요."),
    ("小さいサイズはありますか。", "치-사이 사이즈와 아리마스카.", "작은 사이즈가 있습니까?"),
    ("試着してもいいですか。", "시차쿠시테모 이이데스카.", "입어 봐도 될까요?"),
    ("免税できますか。", "멘제- 데키마스카.", "면세가 가능합니까?"),
    ("領収書をお願いします。", "료-슈-쇼오 오네가이시마스.", "영수증 부탁드립니다."),
    ("この道をまっすぐ行きます。", "코노 미치오 맛스구 이키마스.", "이 길을 쭉 갑니다."),
    ("左に曲がってください。", "히다리니 마갓테 쿠다사이.", "왼쪽으로 돌아 주세요."),
    ("ここで降ろしてください。", "코코데 오로시테 쿠다사이.", "여기에서 내려 주세요."),
    ("チェックアウトをお願いします。", "쳇쿠아우토오 오네가이시마스.", "체크아웃 부탁드립니다."),
    ("荷物を部屋に運んでください。", "니모츠오 헤야니 하콘데 쿠다사이.", "짐을 방으로 옮겨 주세요."),
    ("Wi-Fiのパスワードを教えてください。", "와이파이노 파스와-도오 오시에테 쿠다사이.", "와이파이 비밀번호를 알려 주세요."),
    ("観光地まで何分かかりますか。", "칸코-치마데 난분 카카리마스카.", "관광지까지 몇 분 걸립니까?"),
    ("地図で場所を教えてください。", "치즈데 바쇼오 오시에테 쿠다사이.", "지도에서 장소를 알려 주세요."),
    ("このチケットを使えますか。", "코노 치켓토오 츠카에마스카.", "이 티켓을 사용할 수 있습니까?"),
    ("開店は何時ですか。", "카이텐와 난지데스카.", "개점은 몇 시입니까?"),
    ("閉店は何時ですか。", "헤-텐와 난지데스카.", "폐점은 몇 시입니까?"),
]


def load_data(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "language": "ja",
        "mode": "beginner_sentence_spaced_repetition",
        "daily_new_sentence_count": 7,
        "review_intervals_days": [0, 1, 3, 7, 14, 30],
        "sentences": [],
    }


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def next_date(base: str, days: int) -> str:
    return (parse_date(base) + timedelta(days=days)).isoformat()


# TTS sometimes misreads kanji. Keep the displayed/sent Japanese sentence unchanged,
# but pass this kana reading to the audio generator through `japanese_tts`.
# The list is phrase-based and longest-first so compounds are converted before
# shorter overlapping words.
KANJI_TO_KANA_REPLACEMENTS: dict[str, str] = {
    "搭乗手続き": "とうじょうてつづき",
    "コインロッカー": "コインロッカー",
    "お土産売り場": "おみやげうりば",
    "荷物置き場": "にもつおきば",
    "観光パンフレット": "かんこうパンフレット",
    "お願いします": "おねがいします",
    "何番線": "なんばんせん",
    "待ち合わせます": "まちあわせます",
    "朝食券": "ちょうしょくけん",
    "持っています": "もっています",
    "混んでいます": "こんでいます",
    "二番出口": "にばんでぐち",
    "お会計": "おかいけい",
    "SIMカード": "SIMカード",
    "レストラン": "レストラン",
    "搭乗券": "とうじょうけん",
    "搭乗口": "とうじょうぐち",
    "手荷物": "てにもつ",
    "入国カード": "にゅうこくカード",
    "保安検査": "ほあんけんさ",
    "高速道路": "こうそくどうろ",
    "駐車場": "ちゅうしゃじょう",
    "乗車券": "じょうしゃけん",
    "観光案内": "かんこうあんない",
    "観光地": "かんこうち",
    "案内所": "あんないじょ",
    "案内板": "あんないばん",
    "予約": "よやく",
    "確認": "かくにん",
    "料理": "りょうり",
    "会計": "かいけい",
    "空港": "くうこう",
    "駅員": "えきいん",
    "駅前": "えきまえ",
    "駅の": "えきの",
    "駅まで": "えきまで",
    "駅で": "えきで",
    "駅に": "えきに",
    "駅": "えき",
    "地下鉄": "ちかてつ",
    "電車": "でんしゃ",
    "新宿": "しんじゅく",
    "改札": "かいさつ",
    "出発": "しゅっぱつ",
    "到着": "とうちゃく",
    "出口": "でぐち",
    "入口": "いりぐち",
    "住所": "じゅうしょ",
    "日本語": "にほんご",
    "現金": "げんきん",
    "袋": "ふくろ",
    "一つ": "ひとつ",
    "一杯": "いっぱい",
    "一枚": "いちまい",
    "一本": "いっぽん",
    "二枚": "にまい",
    "二階": "にかい",
    "何時": "なんじ",
    "何分": "なんぷん",
    "何番": "なんばん",
    "何": "なん",
    "店員さん": "てんいんさん",
    "お店": "おみせ",
    "店": "みせ",
    "場所": "ばしょ",
    "地図": "ちず",
    "切符": "きっぷ",
    "券売機": "けんばいき",
    "窓口": "まどぐち",
    "窓側": "まどがわ",
    "席": "せき",
    "写真": "しゃしん",
    "右": "みぎ",
    "左": "ひだり",
    "道": "みち",
    "通り": "とおり",
    "曲がって": "まがって",
    "降ろしてください": "おろしてください",
    "降ります": "おります",
    "乗り換え": "のりかえ",
    "乗ります": "のります",
    "戻りたい": "もどりたい",
    "行きたい": "いきたい",
    "行きます": "いきます",
    "使いたい": "つかいたい",
    "使います": "つかいます",
    "使えます": "つかえます",
    "借りられます": "かりられます",
    "止めても": "とめても",
    "呼んで": "よんで",
    "教えて": "おしえて",
    "見せて": "みせて",
    "見せます": "みせます",
    "見たい": "みたい",
    "見ます": "みます",
    "聞いて": "きいて",
    "聞きます": "ききます",
    "読みます": "よみます",
    "書きます": "かきます",
    "払えます": "はらえます",
    "払います": "はらいます",
    "支払い": "しはらい",
    "別々": "べつべつ",
    "買います": "かいます",
    "買いたい": "かいたい",
    "食べます": "たべます",
    "飲み物": "のみもの",
    "飲んでも": "のんでも",
    "受け取ります": "うけとります",
    "預けても": "あずけても",
    "預けます": "あずけます",
    "運んで": "はこんで",
    "置きます": "おきます",
    "待っています": "まっています",
    "待ちます": "まちます",
    "並べば": "ならべば",
    "撮ります": "とります",
    "取ります": "とります",
    "調べます": "しらべます",
    "探しています": "さがしています",
    "申告します": "しんこくします",
    "迷いました": "まよいました",
    "言って": "いって",
    "帰ります": "かえります",
    "歩いて": "あるいて",
    "送ります": "おくります",
    "渡ります": "わたります",
    "休みます": "やすみます",
    "散歩します": "さんぽします",
    "降りそう": "ふりそう",
    "雨": "あめ",
    "傘": "かさ",
    "薬局": "やっきょく",
    "薬": "くすり",
    "水": "みず",
    "お水": "おみず",
    "お茶": "おちゃ",
    "朝ご飯": "あさごはん",
    "昼ご飯": "ひるごはん",
    "夕食": "ゆうしょく",
    "夕方": "ゆうがた",
    "部屋": "へや",
    "鍵": "かぎ",
    "荷物": "にもつ",
    "大きい": "おおきい",
    "小さい": "ちいさい",
    "近く": "ちかく",
    "少し": "すこし",
    "寒い": "さむい",
    "安く": "やすく",
    "無料": "むりょう",
    "辛い": "からい",
    "辛く": "からく",
    "必要": "ひつよう",
    "時間": "じかん",
    "名前": "なまえ",
    "番号札": "ばんごうふだ",
    "税関": "ぜいかん",
    "免税": "めんぜい",
    "領収書": "りょうしゅうしょ",
    "両替": "りょうがえ",
    "開店": "かいてん",
    "閉店": "へいてん",
    "お土産": "おみやげ",
    "家": "いえ",
    "靴": "くつ",
    "試着": "しちゃく",
    "充電器": "じゅうでんき",
    "タオル": "タオル",
    "エアコン": "エアコン",
    "ホテル": "ホテル",
    "フロント": "フロント",
    "ロビー": "ロビー",
    "バス停": "バスてい",
    "バス": "バス",
    "タクシー": "タクシー",
    "リムジンバス": "リムジンバス",
    "レンタカー": "レンタカー",
    "ガソリン": "ガソリン",
    "満タン": "まんタン",
    "スーツケース": "スーツケース",
    "カード": "カード",
    "チケット": "チケット",
    "メニュー": "メニュー",
    "サイズ": "サイズ",
}


def japanese_tts_reading(japanese: str) -> str:
    tts_text = japanese
    for kanji, kana in sorted(KANJI_TO_KANA_REPLACEMENTS.items(), key=lambda item: len(item[0]), reverse=True):
        tts_text = tts_text.replace(kanji, kana)
    return tts_text


def sentence_signature(japanese: str) -> str:
    """Return a coarse signature to avoid very similar sentence patterns."""
    signature = japanese.strip()
    for token in ("。", "、", "?", "？", " "):
        signature = signature.replace(token, "")
    for prefix in ("この", "その", "あの"):
        signature = signature.replace(prefix, "")
    for suffix in (
        "をお願いします",
        "をください",
        "はどこですか",
        "はどちらですか",
        "は何番ですか",
        "に行きますか",
        "へ行きますか",
        "でできますか",
        "できますか",
        "てもいいですか",
        "たいです",
        "ください",
        "です",
        "ます",
        "か",
    ):
        signature = signature.replace(suffix, "")
    return signature


def is_too_similar(candidate_ja: str, previous_sentences: list[dict[str, Any]]) -> bool:
    candidate_sig = sentence_signature(candidate_ja)
    if len(candidate_sig) < 2:
        return False
    for previous in previous_sentences:
        previous_ja = str(previous.get("japanese") or "")
        if not previous_ja:
            continue
        previous_sig = sentence_signature(previous_ja)
        if candidate_sig == previous_sig:
            return True
        if len(candidate_sig) >= 4 and candidate_sig in previous_sig:
            return True
        if len(previous_sig) >= 4 and previous_sig in candidate_sig:
            return True
    return False


def extract_sent_japanese_from_messages(out_dir: Path, today: str) -> set[str]:
    """Return Japanese sentences already sent in previous daily/replacement messages.

    Some corrected lessons were delivered to Frank and later replaced in the JSON
    learning data.  They must still be treated as already learned so a later
    cron run never reuses them as "today's new sentences".
    """
    sent: set[str] = set()
    if not out_dir.exists():
        return sent
    for path in sorted(out_dir.glob("*_message*.txt")):
        message_date = path.name[:10]
        if message_date >= today:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line in lines:
            marker = "일본어:"
            if marker in line:
                sent.add(line.split(marker, 1)[1].strip())
    return sent


def load_vocab_request(path: Path, today: str) -> dict[str, Any] | None:
    """Return the active user-provided vocabulary request for today's lesson.

    Expected file shape:
    {
      "schema_version": 1,
      "requests": [
        {
          "id": "2026-09-16-frank-words",
          "target_date": "2026-09-16",          # optional; date also works
          "status": "ready",                    # ready/pending are active
          "words": ["温泉", "美術館"],
          "sentences": [
            {
              "japanese": "温泉までバスで行けますか。",
              "pronunciation_ko": "온센마데 바스데 이케마스카.",
              "meaning_ko": "온천까지 버스로 갈 수 있습니까?",
              "required_words": ["温泉"]
            }
          ]
        }
      ]
    }

    The cron script must not silently ignore a ready/pending request.  If Frank
    provides words, a preceding agent step should save natural sentence drafts in
    this queue.  The daily run then validates coverage before sending.
    """
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    requests = payload.get("requests", []) if isinstance(payload, dict) else []
    active: list[dict[str, Any]] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        target_date = str(req.get("target_date") or req.get("date") or "")
        status = str(req.get("status") or "ready")
        if target_date == today and status in {"ready", "pending"}:
            active.append(req)
    if len(active) > 1:
        ids = [str(req.get("id") or "<no-id>") for req in active]
        raise RuntimeError(f"multiple active vocabulary requests for {today}: {ids}")
    return active[0] if active else None


def sentence_required_words(sentence: dict[str, Any], fallback_words: list[str]) -> list[str]:
    raw_words = sentence.get("required_words")
    if raw_words is None:
        raw_words = sentence.get("source_words")
    if raw_words is None:
        raw_words = []
    words = [str(w).strip() for w in raw_words if str(w).strip()]
    if words:
        return words
    japanese = str(sentence.get("japanese") or "")
    return [word for word in fallback_words if word and word in japanese]


def validate_vocab_coverage(vocab_request: dict[str, Any], candidate_sentences: list[dict[str, Any]]) -> dict[str, Any]:
    required_words = [str(w).strip() for w in vocab_request.get("words", []) if str(w).strip()]
    if not required_words:
        raise RuntimeError("active vocabulary request has no words")
    covered: set[str] = set()
    per_sentence: list[dict[str, Any]] = []
    for sentence in candidate_sentences:
        japanese = str(sentence.get("japanese") or "")
        declared = sentence_required_words(sentence, required_words)
        sentence_covered = sorted({word for word in required_words if word in japanese or word in declared})
        covered.update(sentence_covered)
        per_sentence.append({"japanese": japanese, "covered_words": sentence_covered})
    missing = [word for word in required_words if word not in covered]
    if missing:
        raise RuntimeError(f"vocabulary request words not covered by new sentences: {missing}")
    return {
        "id": vocab_request.get("id"),
        "required_words": required_words,
        "covered_words": [word for word in required_words if word in covered],
        "per_sentence": per_sentence,
    }


def select_from_vocab_request(vocab_request: dict[str, Any], daily_count: int) -> list[dict[str, Any]]:
    raw_sentences = vocab_request.get("sentences")
    if not isinstance(raw_sentences, list) or not raw_sentences:
        raise RuntimeError(
            "active vocabulary request needs a 'sentences' list; refusing to fall back to generic daily candidates"
        )
    if len(raw_sentences) != daily_count:
        raise RuntimeError(
            f"active vocabulary request has {len(raw_sentences)} sentences; expected exactly {daily_count}"
        )
    selected: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_sentences, 1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"vocabulary request sentence {idx} must be an object")
        japanese = str(raw.get("japanese") or "").strip()
        pronunciation = str(raw.get("pronunciation_ko") or raw.get("pronunciation") or "").strip()
        meaning = str(raw.get("meaning_ko") or raw.get("korean") or raw.get("meaning") or "").strip()
        if not japanese or not pronunciation or not meaning:
            raise RuntimeError(f"vocabulary request sentence {idx} needs japanese, pronunciation_ko, and meaning_ko")
        selected.append(
            {
                "japanese": japanese,
                "pronunciation_ko": pronunciation,
                "meaning_ko": meaning,
                "required_words": sentence_required_words(raw, [str(w).strip() for w in vocab_request.get("words", [])]),
            }
        )
    validate_vocab_coverage(vocab_request, selected)
    return selected


def choose_new_sentences(
    data: dict[str, Any],
    today: str,
    previously_sent: set[str] | None = None,
    vocab_request: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], bool, dict[str, Any] | None]:
    sentences = data.setdefault("sentences", [])
    todays = [s for s in sentences if s.get("first_learned_date") == today]
    daily_count = int(data.get("daily_new_sentence_count", 7))
    previously_sent = previously_sent or set()
    if len(todays) == daily_count:
        duplicates = [s["japanese"] for s in todays if s.get("japanese") in previously_sent]
        if duplicates:
            raise RuntimeError(f"{today} already has stored new sentences that were sent before: {duplicates}")
        vocab_coverage = None
        if vocab_request:
            vocab_coverage = validate_vocab_coverage(vocab_request, todays)
        return todays, False, vocab_coverage
    if todays:
        raise RuntimeError(f"{today} has {len(todays)} stored new sentences; expected 0 or {daily_count}")

    existing = {s.get("japanese") for s in sentences} | previously_sent
    selected: list[dict[str, Any]] = []
    vocab_coverage = None
    if vocab_request:
        selected = select_from_vocab_request(vocab_request, daily_count)
        vocab_coverage = validate_vocab_coverage(vocab_request, selected)
    else:
        candidates = NEW_SENTENCE_BANK.get(today, TRAVEL_FALLBACK_SENTENCES)
        previous_and_selected = list(sentences)
        for ja, pr, ko in candidates:
            if ja in existing:
                continue
            if is_too_similar(ja, previous_and_selected):
                continue
            selected.append({"japanese": ja, "pronunciation_ko": pr, "meaning_ko": ko})
            previous_and_selected.append({"japanese": ja})
            if len(selected) == daily_count:
                break
        if len(selected) != daily_count:
            raise RuntimeError("not enough non-duplicate travel sentences available")

    next_id_prefix = today.replace("-", "")
    new_items: list[dict[str, Any]] = []
    for i, selected_sentence in enumerate(selected, 1):
        ja = selected_sentence["japanese"]
        if ja in existing:
            raise RuntimeError(f"new sentence duplicates an already learned/sent sentence: {ja}")
        if not vocab_request and is_too_similar(ja, sentences):
            raise RuntimeError(f"new sentence is too similar to an already learned sentence: {ja}")
        item = {
            "id": f"ja-{next_id_prefix}-{i:03d}",
            "japanese": ja,
            "pronunciation_ko": selected_sentence["pronunciation_ko"],
            "meaning_ko": selected_sentence["meaning_ko"],
            "first_learned_date": today,
            "next_review_date": next_date(today, 1),
            "review_stage": 1,
            "history": [],
        }
        if vocab_request:
            item["source_vocab_request_id"] = vocab_request.get("id")
            item["required_words"] = sentence_required_words(selected_sentence, vocab_coverage["required_words"] if vocab_coverage else [])
        sentences.append(item)
        new_items.append(item)
    if vocab_request:
        vocab_coverage = validate_vocab_coverage(vocab_request, new_items)
    return new_items, True, vocab_coverage


def select_due_reviews(data: dict[str, Any], today: str) -> list[dict[str, Any]]:
    # Select before any advancement and never include today's new sentences.
    due = [
        s
        for s in data.get("sentences", [])
        if s.get("first_learned_date") != today
        and s.get("next_review_date", "9999-99-99") <= today
        and not any(
            h.get("date") == today and h.get("result") == "read_review"
            for h in s.get("history", [])
        )
    ]
    return sorted(due, key=lambda s: (s.get("first_learned_date", ""), s.get("id", "")), reverse=True)


def advance_reviews(data: dict[str, Any], reviews: list[dict[str, Any]], today: str) -> None:
    intervals = data.get("review_intervals_days", [0, 1, 3, 7, 14, 30])
    included_ids = {s.get("id") for s in reviews}
    for s in data.get("sentences", []):
        if s.get("id") not in included_ids:
            continue
        old_stage = int(s.get("review_stage", 0))
        new_stage = min(old_stage + 1, len(intervals) - 1)
        s["review_stage"] = new_stage
        s["next_review_date"] = next_date(today, int(intervals[new_stage]))
        s.setdefault("history", []).append(
            {
                "date": today,
                "result": "read_review",
                "note": "읽기 복습으로 보낸 뒤 자동으로 다음 복습 단계로 이동",
            }
        )


def build_message(today: str, new_items: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> str:
    weekday_ko = ["월", "화", "수", "목", "금", "토", "일"][parse_date(today).weekday()]
    lines: list[str] = []
    lines.append("프랭크, 좋은 아침입니다. 엘르 선생님입니다. 🌿")
    lines.append(f"{weekday_ko}요일 아침도 일본어 문장 {len(new_items)}개만 편하게 익혀 보겠습니다.")
    lines.append("")
    lines.append(f"오늘 새 문장 {len(new_items)}개")
    for i, s in enumerate(new_items, 1):
        lines.append(f"{i}. 일본어: {s['japanese']}")
        lines.append(f"   발음: {s['pronunciation_ko']}")
        lines.append(f"   뜻: {s['meaning_ko']}")
    lines.append("")
    lines.append("음성")
    lines.append("- 첨부 파일을 들으시면 됩니다.")
    lines.append("- 재생 순서: 한국어 뜻 1번 → 일본어 문장 3번")
    if reviews:
        lines.append("")
        lines.append("오늘 복습 문장")
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for s in reviews:
            grouped[s["first_learned_date"]].append(s)
        for learned_date in sorted(grouped, reverse=True):
            lines.append(f"[{learned_date} 학습]")
            for i, s in enumerate(grouped[learned_date], 1):
                lines.append(f"{i}. 일본어: {s['japanese']}")
                lines.append(f"   발음: {s['pronunciation_ko']}")
                lines.append(f"   뜻: {s['meaning_ko']}")
    lines.append("")
    lines.append("첨부된 음성을 들으면서 입으로 한 번 따라 해 주세요. 잘하고 계십니다, 프랭크.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--vocab-requests", type=Path, default=DEFAULT_VOCAB_REQUESTS_PATH)
    parser.add_argument("--reliability-db", type=Path, default=DEFAULT_RELIABILITY_DB_PATH)
    parser.add_argument("--disable-reliability", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and validate artifacts without writing learning progress or delivery-ready outbox state.",
    )
    args = parser.parse_args()

    data = load_data(args.data)
    daily_count = int(data.get("daily_new_sentence_count", 7))
    vocab_request = load_vocab_request(args.vocab_requests, args.date)

    reliability_conn = None
    reliability_execution = None
    reliability_policy_version = None
    reliability_request_id = None
    reliability_spec: dict[str, Any] = {}
    if not args.disable_reliability:
        reliability_conn = reliability.connect(args.reliability_db)
        reliability_policy_version = reliability.ensure_default_policy(
            reliability_conn,
            sentence_count=daily_count,
        )
        if vocab_request:
            stored_request = reliability.import_vocab_request(
                reliability_conn,
                vocab_request,
                target_date=args.date,
            )
            reliability_request_id = str(stored_request["request_id"])
        _, effective_policy = reliability.get_effective_policy(
            reliability_conn,
            agent=reliability.DEFAULT_AGENT,
            task=reliability.DEFAULT_TASK,
            target_date=args.date,
        )
        reliability_spec = {
            "agent": reliability.DEFAULT_AGENT,
            "task": reliability.DEFAULT_TASK,
            "target_date": args.date,
            "timezone": "Asia/Seoul",
            "sentence_count": daily_count,
            "required_words": [str(w).strip() for w in (vocab_request or {}).get("words", []) if str(w).strip()],
            "request_id": reliability_request_id,
            "policy_version": reliability_policy_version,
            "policy": effective_policy,
            "source_vocab_request_id": (vocab_request or {}).get("id"),
        }
        reliability_execution = reliability.begin_execution(
            reliability_conn,
            agent=reliability.DEFAULT_AGENT,
            task=reliability.DEFAULT_TASK,
            target_date=args.date,
            spec=reliability_spec,
            request_id=reliability_request_id,
            policy_version=reliability_policy_version,
        )

    previously_sent = extract_sent_japanese_from_messages(args.out_dir, args.date)
    new_items, added_new, vocab_coverage = choose_new_sentences(data, args.date, previously_sent, vocab_request)
    due_reviews = select_due_reviews(data, args.date)
    # Guardrail: preserve the exact selected review list for the message, then advance only those IDs.
    if not args.dry_run:
        advance_reviews(data, due_reviews, args.date)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    audio_json = args.out_dir / f"{args.date}_sentences.json"
    message_path = args.out_dir / f"{args.date}_message.txt"
    audit_path = args.out_dir / f"{args.date}_review_audit.json"

    audio_json.write_text(
        json.dumps(
            {
                "items": [
                    {
                        "sentence_id": s["id"],
                        "japanese": s["japanese"],
                        "japanese_tts": s["japanese"],
                        "korean": s["meaning_ko"],
                    }
                    for s in new_items
                ]
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    message = build_message(args.date, new_items, due_reviews)
    message_path.write_text(message, encoding="utf-8")

    if not args.dry_run:
        args.data.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    groups: dict[str, int] = defaultdict(int)
    for s in due_reviews:
        groups[s["first_learned_date"]] += 1
    reliability_validated = None
    reliability_execution_id = reliability_execution.id if reliability_execution else None
    reliability_outbox_status = None
    if reliability_conn is not None and reliability_execution is not None:
        audio_payload = json.loads(audio_json.read_text(encoding="utf-8"))
        with reliability_conn:
            reliability.add_artifact(
                reliability_conn,
                reliability_execution.id,
                artifact_kind="message_text",
                path=message_path,
                content_json={"new_sentence_ids": [s["id"] for s in new_items]},
            )
            reliability.add_artifact(
                reliability_conn,
                reliability_execution.id,
                artifact_kind="audio_input_json",
                path=audio_json,
                content_json=audio_payload,
            )
            reliability_validated = reliability.validate_sentence_bundle(
                execution_id=reliability_execution.id,
                conn=reliability_conn,
                spec=reliability_spec,
                new_items=new_items,
                message=message,
                audio_payload=audio_payload,
                vocab_coverage=vocab_coverage,
            )
            if args.dry_run:
                reliability.cancel_outbox_for_execution(
                    reliability_conn,
                    reliability_execution.id,
                    "superseded by dry-run validation; no delivery intent should remain active",
                )
                reliability.mark_execution_status(
                    reliability_conn,
                    reliability_execution.id,
                    "dry_run_validated" if reliability_validated else "validation_failed",
                )
            else:
                reliability.finalize_generation(
                    reliability_conn,
                    reliability_execution.id,
                    validated=reliability_validated,
                    content=message,
                    media=[str(audio_json)],
                )
        reliability_outbox_status = "dry_run" if args.dry_run and reliability_validated else ("fake_ready" if reliability_validated else "blocked")
        if not reliability_validated:
            raise RuntimeError("required reliability validation failed; refusing to report generated lesson as send-ready")

    audit = {
        "date": args.date,
        "new_count": len(new_items),
        "added_new": added_new,
        "review_count": len(due_reviews),
        "review_ids": [s["id"] for s in due_reviews],
        "review_groups": dict(sorted(groups.items(), reverse=True)),
        "message_path": str(message_path),
        "audio_json": str(audio_json),
        "audio_japanese_tts_mode": "original_display_sentence",
        "vocab_request_id": vocab_request.get("id") if vocab_request else None,
        "vocab_coverage": vocab_coverage,
        "reliability_db": None if args.disable_reliability else str(args.reliability_db),
        "reliability_execution_id": reliability_execution_id,
        "reliability_policy_version": reliability_policy_version,
        "reliability_request_id": reliability_request_id,
        "reliability_validated": reliability_validated,
        "reliability_outbox_status": reliability_outbox_status,
        "dry_run": args.dry_run,
    }
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({**audit, "audit_path": str(audit_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
