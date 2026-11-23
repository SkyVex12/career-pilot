"""Reviewed fixed post-call chrome, independent of the spoken-language catalog.

Generated answers, feedback and summaries follow the selected language. A language
without a reviewed fixed-label translation uses the existing English chrome in the
renderer; do not claim an expanded speech catalog also translates the interface.
"""
# ruff: noqa: RUF001 -- Native language punctuation and letters are intentional.

LABELS = {
    "en": (
        "Strengths|To improve|How it went|Better|Subject|Yesterday|To"
        "day|Blockers|Commitments|Decisions|Action items|due|none"
    ),
    "es": (
        "Fortalezas|Para mejorar|Cómo fue|Mejor respuesta|Asunto|Ayer"
        "|Hoy|Bloqueos|Compromisos|Decisiones|Acciones|vence|ninguno"
    ),
    "vi": (
        "Điểm mạnh|Cần cải thiện|Kết quả|Câu trả lời tốt hơn|Chủ đề|H"
        "ôm qua|Hôm nay|Trở ngại|Cam kết|Quyết định|Việc cần làm|hạn|"
        "không có"
    ),
    "zh": ("优势|改进建议|表现|更好的回答|主题|昨天|今天|阻碍|承诺|决定|待办事项|截止|无"),
    "hi": (
        "खूबियाँ|सुधार के सुझाव|प्रदर्शन|बेहतर उत्तर|विषय|कल|आज|बाधाए"
        "ँ|प्रतिबद्धताएँ|निर्णय|कार्य सूची|समय सीमा|कोई नहीं"
    ),
    "ar": (
        "نقاط القوة|مجالات التحسين|الأداء|إجابة أفضل|الموضوع|أمس|اليو"
        "م|العوائق|الالتزامات|القرارات|المهام|الموعد النهائي|لا يوجد"
    ),
    "fr": (
        "Points forts|À améliorer|Déroulement|Meilleure réponse|Objet"
        "|Hier|Aujourd’hui|Blocages|Engagements|Décisions|Actions à m"
        "ener|échéance|aucun"
    ),
    "bn": (
        "দক্ষতা|উন্নতির পরামর্শ|কেমন হয়েছে|আরও ভালো উত্তর|বিষয়|গতকা"
        "ল|আজ|বাধা|অঙ্গীকার|সিদ্ধান্ত|করণীয়|সময়সীমা|কিছু নেই"
    ),
    "pt": (
        "Pontos fortes|A melhorar|Desempenho|Resposta melhor|Assunto|"
        "Ontem|Hoje|Impedimentos|Compromissos|Decisões|Ações|prazo|ne"
        "nhum"
    ),
    "ru": (
        "Сильные стороны|Что улучшить|Как прошло|Лучший ответ|Тема|Вч"
        "ера|Сегодня|Препятствия|Обязательства|Решения|Задачи|срок|не"
        "т"
    ),
    "ur": (
        "خوبیاں|بہتری کی تجاویز|کارکردگی|بہتر جواب|موضوع|کل|آج|رکاوٹی"
        "ں|وعدے|فیصلے|کام|آخری تاریخ|کوئی نہیں"
    ),
    "id": (
        "Kelebihan|Perlu ditingkatkan|Hasil|Jawaban lebih baik|Subjek"
        "|Kemarin|Hari ini|Hambatan|Komitmen|Keputusan|Tindak lanjut|"
        "tenggat|tidak ada"
    ),
    "de": (
        "Stärken|Verbesserungen|Verlauf|Bessere Antwort|Betreff|Geste"
        "rn|Heute|Hindernisse|Zusagen|Entscheidungen|Aufgaben|fällig|"
        "keine"
    ),
    "ja": (
        "強み|改善点|振り返り|より良い回答|件名|昨日|今日|障害|約束|決定事項|対応事項|期限|なし"
    ),
    "tr": (
        "Güçlü yönler|Geliştirilecek alanlar|Performans|Daha iyi yanı"
        "t|Konu|Dün|Bugün|Engeller|Taahhütler|Kararlar|Yapılacaklar|s"
        "on tarih|yok"
    ),
    "ta": (
        "பலங்கள்|மேம்படுத்த வேண்டியவை|செயல்திறன்|சிறந்த பதில்|தலைப்பு"
        "|நேற்று|இன்று|தடைகள்|உறுதிமொழிகள்|முடிவுகள்|செயல்பாடுகள்|கால"
        "க்கெடு|எதுவுமில்லை"
    ),
    "te": (
        "బలాలు|మెరుగుదల సూచనలు|పనితీరు|మెరుగైన సమాధానం|విషయం|నిన్న|ఈర"
        "ోజు|అడ్డంకులు|హామీలు|నిర్ణయాలు|చేయవలసిన పనులు|గడువు|ఏమీ లేదు"
    ),
    "mr": (
        "बलस्थाने|सुधारणा|कामगिरी|अधिक चांगले उत्तर|विषय|काल|आज|अडथळे"
        "|वचनबद्धता|निर्णय|कृती|अंतिम मुदत|काहीही नाही"
    ),
    "ko": (
        "강점|개선점|진행 결과|더 나은 답변|제목|어제|오늘|장애물|"
        "약속|결정 사항|실행 항목|기한|없음"
    ),
    "it": (
        "Punti di forza|Da migliorare|Com’è andata|Risposta migliore|"
        "Oggetto|Ieri|Oggi|Ostacoli|Impegni|Decisioni|Azioni|scadenza"
        "|nessuno"
    ),
    "fa": (
        "نقاط قوت|موارد بهبود|عملکرد|پاسخ بهتر|موضوع|دیروز|امروز|موان"
        "ع|تعهدات|تصمیم‌ها|اقدامات|مهلت|هیچ‌کدام"
    ),
    "th": (
        "จุดแข็ง|สิ่งที่ควรปรับปรุง|ผลการสนทนา|คำตอบที่ดีขึ้น|หัวข้อ|"
        "เมื่อวาน|วันนี้|อุปสรรค|คำมั่นสัญญา|การตัดสินใจ|สิ่งที่ต้องท"
        "ำ|กำหนดส่ง|ไม่มี"
    ),
    "sw": (
        "Nguvu|Mambo ya kuboresha|Matokeo|Jibu bora|Mada|Jana|Leo|Vik"
        "wazo|Ahadi|Maamuzi|Hatua za kuchukua|tarehe ya mwisho|hakuna"
    ),
    "pa": (
        "ਖੂਬੀਆਂ|ਸੁਧਾਰ ਲਈ ਸੁਝਾਅ|ਪ੍ਰਦਰਸ਼ਨ|ਵਧੀਆ ਜਵਾਬ|ਵਿਸ਼ਾ|ਕੱਲ੍ਹ|ਅੱਜ|ਰੁਕਾਵਟਾਂ|ਵਾਅਦੇ|ਫ਼ੈਸਲੇ|ਕਰਨ ਵਾਲੇ ਕੰਮ|ਆਖਰੀ ਮਿਤੀ|ਕੋਈ ਨਹੀਂ"
    ),
}

METADATA = {
    "es": (
        "Llamada de unos {minutes} minutos. {questions} preguntas det"
        "ectadas.\n\nNo se guardó la transcripción de esta sesión; no h"
        "ay resumen escrito."
    ),
    "vi": (
        "Cuộc gọi khoảng {minutes} phút. Phát hiện {questions} câu hỏ"
        "i.\n\nBản chép lời không được lưu cho phiên này nên không có b"
        "ản tóm tắt."
    ),
    "zh": (
        "通话约 {minutes} 分钟，检测到 {questions} 个问题。\n\n"
        "本次会话未保存转录，因此没有文字摘要。"
    ),
    "hi": (
        "कॉल लगभग {minutes} मिनट की थी। {questions} प्रश्न मिले।\n\nइस "
        "सत्र का प्रतिलेख सहेजा नहीं गया, इसलिए लिखित सारांश उपलब्ध न"
        "हीं है।"
    ),
    "ar": (
        "استمرت المكالمة نحو {minutes} دقيقة. تم رصد {questions} سؤال"
        ".\n\nلم يُحفظ نص هذه الجلسة، لذلك لا يوجد ملخص مكتوب."
    ),
    "fr": (
        "Appel d’environ {minutes} minutes. {questions} questions dét"
        "ectées.\n\nLa transcription de cette session n’a pas été conse"
        "rvée ; aucun résumé écrit n’est disponible."
    ),
    "bn": (
        "কলটি প্রায় {minutes} মিনিটের ছিল। {questions}টি প্রশ্ন শনাক"
        "্ত হয়েছে।\n\nএই সেশনের প্রতিলিপি সংরক্ষিত হয়নি, তাই লিখিত সা"
        "রাংশ নেই।"
    ),
    "pt": (
        "Chamada de cerca de {minutes} minutos. {questions} perguntas"
        " detectadas.\n\nA transcrição desta sessão não foi guardada; n"
        "ão há resumo escrito."
    ),
    "ru": (
        "Звонок длился около {minutes} минут. Обнаружено вопросов: {q"
        "uestions}.\n\nСтенограмма сеанса не сохранялась, поэтому письм"
        "енного резюме нет."
    ),
    "ur": (
        "کال تقریباً {minutes} منٹ کی تھی۔ {questions} سوالات ملے۔\n\nا"
        "س نشست کی نقل محفوظ نہیں کی گئی، اس لیے تحریری خلاصہ موجود ن"
        "ہیں ہے۔"
    ),
    "id": (
        "Panggilan sekitar {minutes} menit. {questions} pertanyaan te"
        "rdeteksi.\n\nTranskrip sesi ini tidak disimpan sehingga tidak "
        "ada ringkasan tertulis."
    ),
    "de": (
        "Gespräch von etwa {minutes} Minuten. {questions} Fragen erka"
        "nnt.\n\nDas Transkript wurde nicht gespeichert; es gibt keine "
        "schriftliche Zusammenfassung."
    ),
    "ja": (
        "通話時間は約 {minutes} 分です。{questions} 件の質問を検出しました。\n\n"
        "このセッションの文字起こし"
        "は保存されていないため、文章の要約はありません。"
    ),
    "tr": (
        "Görüşme yaklaşık {minutes} dakika sürdü. {questions} soru al"
        "gılandı.\n\nBu oturumun dökümü kaydedilmediği için yazılı özet"
        " yoktur."
    ),
    "ta": (
        "அழைப்பு சுமார் {minutes} நிமிடங்கள் நீடித்தது. {questions} க"
        "ேள்விகள் கண்டறியப்பட்டன.\n\nஇந்த அமர்வின் உரை சேமிக்கப்படவில்ல"
        "ை; எழுத்துச் சுருக்கம் இல்லை."
    ),
    "te": (
        "కాల్ సుమారు {minutes} నిమిషాలు కొనసాగింది. {questions} ప్రశ్"
        "నలు గుర్తించబడ్డాయి.\n\nఈ సెషన్ లిప్యంతరీకరణ సేవ్ చేయబడలేదు కా"
        "బట్టి వ్రాతపూర్వక సారాంశం లేదు."
    ),
    "mr": (
        "कॉल सुमारे {minutes} मिनिटांचा होता. {questions} प्रश्न आढळल"
        "े.\n\nया सत्राचा मजकूर जतन केला नसल्यामुळे लेखी सारांश नाही."
    ),
    "ko": (
        "통화 시간은 약 {minutes}분입니다. 질문 {questions}개를 감지했습니다.\n\n이 세션의 대화록을"
        " 저장하지 않아 서면 요약이 없습니다."
    ),
    "it": (
        "Chiamata di circa {minutes} minuti. Rilevate {questions} dom"
        "ande.\n\nLa trascrizione non è stata conservata; non è disponi"
        "bile un riepilogo scritto."
    ),
    "fa": (
        "تماس حدود {minutes} دقیقه طول کشید. {questions} پرسش شناسایی"
        " شد.\n\nمتن این جلسه ذخیره نشده است؛ بنابراین خلاصهٔ نوشتاری و"
        "جود ندارد."
    ),
    "th": (
        "การสนทนาประมาณ {minutes} นาที ตรวจพบ {questions} คำถาม\n\nไม่ไ"
        "ด้บันทึกข้อความของเซสชันนี้ จึงไม่มีสรุปเป็นลายลักษณ์อักษร"
    ),
    "sw": (
        "Simu ya takribani dakika {minutes}. Maswali {questions} yame"
        "gunduliwa.\n\nNakala ya kikao hiki haikuhifadhiwa, kwa hiyo ha"
        "kuna muhtasari ulioandikwa."
    ),
    "pa": (
        "ਕਾਲ ਲਗਭਗ {minutes} ਮਿੰਟ ਦੀ ਸੀ। {questions} ਸਵਾਲ ਮਿਲੇ।\n\nਇਸ ਸੈ"
        "ਸ਼ਨ ਦਾ ਲਿਖਤੀ ਰਿਕਾਰਡ ਸੰਭਾਲਿਆ ਨਹੀਂ ਗਿਆ, ਇਸ ਲਈ ਲਿਖਤੀ ਸਾਰ ਨਹੀਂ ਹ"
        "ੈ।"
    ),
}
