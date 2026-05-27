# TODO: Move to frontend
MODALS = {
    "help": {
        "EN": {
            "help_title": "Help",
            "help_text": (
                "<p>Welcome to the interview.</p>"
                "<p>You can send messages by typing them in the text box and clicking the send button or pressing the keyboard shortcut displayed.</p>"
                "<p>If you hover on a question from the interviewer, you can skip the question, or provide feedback on the question by giving it a thumbs up or thumbs down.</p>"
                "The interview can be continued at a later time, however if you wish to end the interview, you can do so by clicking the exit button."
                '<p>To conduct the interview, we have used a so called large language model, which generates questions based on our instructions and the conversation. The capabilities of these models also depend on the training data originally used to train the algorithm, as well as the specific architecture applied. In this interview, we have chosen <code style="display:inline-block">{model}</code> as the underlying language model.</p>'
                '<p>If you have any questions, corrections, or concerns regarding AIntervieweren, please contact us at <a href="mailto:{email}">{email}</a>.</p>'
            ),
        },
        "DA": {
            "help_title": "Hjælp",
            "help_text": (
                "<p>Velkommen til interviewet.</p>"
                "<p>Du kan sende beskeder ved at skrive dem i tekstboksen og klikke på send-knappen eller ved at bruge den viste tastaturgenvej.</p>"
                "<p>Hvis du holder musen over et spørgsmål fra intervieweren, kan du springe spørgsmålet over eller give feedback ved at give det en thumbs up eller thumbs down.</p>"
                "<p>Interviewet kan fortsættes på et senere tidspunkt, men hvis du ønsker at afslutte interviewet, kan du gøre det ved at klikke på exit-knappen.</p>"
                '<p>Til at udføre interviewet har vi anvendt en stor sprogmodel, der på baggrund af vores instruktioner og samtalen generere spørgsmål. Disse modellers kunnen afhænger herudover også af den træningsdata der er blevet brugt oprindeligt til at træne algoritmen, samt den specifikke arkitektur der er anvendt. I dette interview har vi valgt <code style="display:inline-block">{model}</code> som den underliggende sprogmodel.</p>'
                '<p>Hvis du har spørgsmål, rettelser eller bekymringer vedrørende AIntervieweren, bedes du kontakte os på <a href="mailto:{email}">{email}</a>.</p>'
            ),
        },
    },
    "exit": {
        "EN": {
            "exit_title": "Exit",
            "exit_text": (
                "<p>Are you sure you want to exit the interview?</p>"
                "<p>This cannot be undone and will disable the possibility to continue the interview at a later time.</p>"
            ),
            "exit_button": "Exit",
        },
        "DA": {
            "exit_title": "Afslut",
            "exit_text": (
                "<p>Er du sikker på, at du vil afslutte interviewet?</p>"
                "<p>Dette kan ikke fortrydes og vil deaktivere muligheden for at fortsætte interviewet på et senere tidspunkt.</p>"
            ),
            "exit_button": "Afslut",
        },
    },
    "consent": {
        "EN": {
            "title": "Consent",
            "text": (
                "<p>Welcome to AInterviewer</p>"
                "<p>By participating in this interview, you must accept the following conditions.</p>"
            ),
            "accept": "Accept",
            "decline": "Decline",
        },
        "DA": {
            "accept": "Accepter",
            "decline": "Afvis",
            "title": "Brugsvilkår",
            "text": (
                "<p>Velkommen til AInterviewer</p>"
                "<p>Ved at bruge denne service accepterer du følgende vilkår:</p>"
            ),
        },
    },
    "welcome": {
        "EN": {
            "section_before_id": 'If you wish to withdraw your consent or change your answers, please contact <a class="contact-email" href="{email}">{email}</a> with a reference to the following code:',
            "section_after_id": "It is your own responsibility to store this code securely before starting the interview. It is the only way for us to identify and modify or delete your data.",
        },
        "DA": {
            "section_before_id": 'Hvis du ønsker at trække dit samtykke tilbage eller ændre dine svar, bedes du kontakte <a class="contact-email" href="{email}">{email}</a> med en reference til følgende kode:',
            "section_after_id": "Det er dit eget ansvar at opbevare denne kode sikkert, før du påbegynder interviewet. Det er den eneste måde, hvorpå vi kan identificere og ændre eller slette dine data.",
        },
    },
}
