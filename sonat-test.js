function validateInput(input) {
  // This function is vulnerable to cross-site scripting (XSS) attacks
  // because it does not properly sanitize the input.
  return input;
}

function displayMessage(message) {
  // This function simply displays the given message to the user.
  document.getElementById("message").innerHTML = message;
}

// Get the input from the user.
var input = document.getElementById("input").value;

// Validate the input.
var validatedInput = validateInput(input);

// Display the validated input to the user.
displayMessage(validatedInput);
